#!/usr/bin/env python3
"""Authenticated, resumable GitHub Actions -> original Temu template."""
import argparse
import csv
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import requests

from paolo import BASE, Client, ScraperError, listing_links, parse_product, site_url, stock_state
from temu import Template, map_product, classify

ROOT = Path(__file__).resolve().parent


def csv_file(path, records, fields):
    with path.open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        for row in records:
            cooked = {}
            for field in fields:
                value = row.get(field, '')
                if isinstance(value, (dict, list)):
                    value = json.dumps(value, ensure_ascii=False)
                value = str(value)
                if value.lstrip().startswith(('=', '+', '-', '@')):
                    value = "'" + value
                cooked[field] = value
            writer.writerow(cooked)


def write_templates(schema, rows, output, prefix):
    if not rows:
        return []
    files = []
    for offset in range(0, len(rows), 9996):
        suffix = '' if len(rows) <= 9996 else f'_{offset // 9996 + 1:03d}'
        path = output / f'{prefix}{suffix}.xlsx'
        schema.write(rows[offset:offset + 9996], path)
        files.append(path.name)
    return files


def make_exports(schema, config, products, output, skipped, crawl):
    ready, draft, audit = [], [], []
    filtered = []
    excluded_urls = {item['url'] for item in skipped}
    for product in products:
        status = stock_state(product)
        category, _ = classify(product, config, schema)
        if status == 'in_stock' and category in schema.categories:
            filtered.append(product)
        elif product['url'] not in excluded_urls:
            reason = 'category_not_selected' if status == 'in_stock' else status
            skipped.append({'sku': product['sku'], 'url': product['url'], 'reason': reason})
            excluded_urls.add(product['url'])
    products = filtered
    for product in products:
        # Re-filter saved checkpoints too: an old exporter must never bypass stock rules.
        if stock_state(product) != 'in_stock':
            continue
        values, errors, evidence = map_product(product, config, schema)
        draft.append(values)
        audit.append({'sku': product['sku'], 'name': product['name'], 'url': product['url'],
                      'category': values.get('E', ''), 'price_eur': values.get('QZ', ''),
                      'availability': product.get('availability', ''), 'status': 'REVIEW' if errors else 'READY',
                      'missing_or_invalid': ' | '.join(errors), 'evidence': evidence})
        if not errors:
            ready.append(values)
    # Remove stale templates from restored artifacts before emitting current files.
    for glob in ['temu_ready*.xlsx', 'temu_draft*.xlsx']:
        for path in output.glob(glob):
            path.unlink()
    files = write_templates(schema, draft, output, 'temu_draft')
    files += write_templates(schema, ready, output, 'temu_ready')
    csv_file(output / 'validation_report.csv', audit, ['sku', 'name', 'url', 'category', 'price_eur',
             'availability', 'status', 'missing_or_invalid', 'evidence'])
    missing_fields = []
    for row in audit:
        for issue in row['missing_or_invalid'].split(' | '):
            if not issue:
                continue
            col = re.match(r'^([A-Z]{1,3})\s', issue)
            code = col[1] if col else ''
            missing_fields.append({'sku': row['sku'], 'category': row['category'], 'column': code,
                'field': schema.headers.get(code, ''), 'issue': issue, 'url': row['url']})
    csv_file(output / 'missing_fields.csv', missing_fields, ['sku', 'category', 'column', 'field', 'issue', 'url'])
    prices = []
    for product in products:
        category, _ = classify(product, config, schema)
        prices.append({**product, 'temu_category': category, 'temu_category_name': schema.categories[category]})
    csv_file(output / 'products_with_prices.csv', prices, ['sku', 'name', 'category', 'temu_category', 'temu_category_name', 'brand', 'price',
             'currency', 'price_source', 'price_raw', 'availability', 'purchase_limit',
             'description', 'images', 'options_json', 'url', 'fetched_at'])
    csv_file(output / 'excluded.csv', skipped, ['sku', 'url', 'reason'])
    summary = {'in_stock_products': len(products), 'ready': len(ready), 'draft': len(draft),
               'selected_category_products': len(products),
               'category_not_selected': sum(s.get('reason') == 'category_not_selected' for s in skipped),
               'review': len(draft) - len(ready), 'missing_price': sum(not p.get('price') for p in products),
               'excluded': len(skipped), 'files': files, **crawl,
               'finished_at': datetime.now(timezone.utc).isoformat()}
    (output / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    summary_path = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary_path:
        with open(summary_path, 'a', encoding='utf-8') as f:
            f.write(f'## Paolo → Temu\n\nIn stock: **{len(products)}**. Ready: **{len(ready)}**. '
                    f'For review: **{len(draft)-len(ready)}**. Excluded: **{len(skipped)}**.\n\n'
                    'Download the **paolo-temu-results** artifact. Upload only `temu_ready*.xlsx` to Temu. '
                    '`temu_draft*.xlsx` contains rows that may need more information; see `validation_report.csv`.\n\n')
            if crawl.get('time_limit_reached'):
                f.write('Time limit reached. Run again with this workflow run ID as `resume_run_id`.\n')
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--start', action='append')
    parser.add_argument('--limit', type=int, default=5, help='In-stock products to collect; 0 = all')
    parser.add_argument('--max-minutes', type=float, default=240)
    parser.add_argument('--output', type=Path, default=ROOT / 'output')
    parser.add_argument('--config', type=Path, default=ROOT / 'config.json')
    parser.add_argument('--template', type=Path, default=ROOT / 'templates/temu_template.xlsx')
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--public', action='store_true', help='Local verification only; no prices')
    parser.add_argument('--export-only', action='store_true', help='Re-map an existing checkpoint without network')
    args = parser.parse_args()
    if args.limit < 0 or not 0 < args.max_minutes <= 300:
        parser.error('limit must be >= 0; max-minutes must be > 0 and <= 300')
    config = json.loads(args.config.read_text(encoding='utf-8'))
    schema = Template(args.template)
    starts = [site_url(s) for s in (args.start or [BASE + 'produkti'])]
    args.output.mkdir(parents=True, exist_ok=True)
    metadata = args.output / 'crawl_state.json'
    log = args.output / 'checkpoint.jsonl'
    profile = {'starts': starts, 'public': args.public,
               'credential_identity': hashlib.sha256(os.environ.get('PAOLO_EMAIL', '').strip().casefold().encode()).hexdigest()}
    products, skipped, visited = [], [], set()
    resume = args.resume or args.export_only
    state = {}
    if resume and metadata.exists():
        state = json.loads(metadata.read_text(encoding='utf-8'))
        # export-only is explicitly offline and can use a checkpoint made by another account.
        if state.get('profile') != profile and not args.export_only:
            raise ScraperError('Checkpoint е от друг адрес, режим или профил. Изберете ново изпълнение без resume_run_id.')
        if log.exists():
            for line in log.read_text(encoding='utf-8').splitlines():
                if not line.strip():
                    continue
                event = json.loads(line)
                visited.add(event['url'])
                current_product = event.get('product')
                category = classify(current_product, config, schema)[0] if current_product else ''
                if current_product and stock_state(current_product) == 'in_stock' and category in schema.categories:
                    products.append(event['product'])
                elif event.get('product'):
                    status = stock_state(event['product'])
                    skipped.append({'sku': event['product']['sku'], 'url': event['url'],
                                    'reason': 'category_not_selected' if status == 'in_stock' else status})
                else:
                    skipped.append(event)
    if args.export_only:
        make_exports(schema, config, products, args.output, skipped, {'export_only': True})
        return 0
    if not resume:
        log.write_text('', encoding='utf-8')
    # Do not re-upload stale ready files if a resumed run fails before login.
    for pattern in ['temu_ready*.xlsx', 'temu_draft*.xlsx', 'products_with_prices.csv',
                    'validation_report.csv', 'missing_fields.csv', 'excluded.csv', 'summary.json']:
        for path in args.output.glob(pattern):
            path.unlink()
    client = Client(float(config.get('request_delay_seconds', 1.5)))
    if not args.public:
        email, password = os.environ.get('PAOLO_EMAIL', ''), os.environ.get('PAOLO_PASSWORD', '')
        if not email or not password:
            raise ScraperError('Добавете GitHub Secrets PAOLO_EMAIL и PAOLO_PASSWORD.')
        try:
            client.login(email, password)
        except (ScraperError, requests.RequestException):
            (args.output / 'summary.json').write_text(json.dumps({'crawl_error': 'B2B login failed',
                'ready': 0, 'files': []}, indent=2), encoding='utf-8')
            raise
        del password
        print('B2B входът е потвърден. Цените се проверяват за всеки продукт.', flush=True)
    deadline = time.monotonic() + args.max_minutes * 60
    time_limit, error, pages = False, '', 0
    cursor = state.get('cursor') if resume else None
    start_index = int(state.get('start_index', 0)) if resume else 0
    complete = False
    seen_skus = {p['sku'] for p in products}
    def save_state(index, page):
        payload = {'profile': profile, 'cursor': page, 'start_index': index,
                   'updated_at': datetime.now(timezone.utc).isoformat()}
        tmp = metadata.with_suffix('.tmp.json')
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        tmp.replace(metadata)
    try:
        with log.open('a', encoding='utf-8') as checkpoint:
            for index in range(start_index, len(starts)):
                page = cursor if index == start_index and cursor else starts[index]
                seen_pages = set()
                while page:
                    if time.monotonic() >= deadline:
                        time_limit = True
                        break
                    if args.limit and len(products) >= args.limit:
                        break
                    if page in seen_pages:
                        raise ScraperError('Повтаряща се страница: сканирането е спряно за проверка.')
                    seen_pages.add(page)
                    save_state(index, page)
                    html = client.get(page)
                    pages += 1
                    direct = urlsplit(page).path.startswith('/pr-')
                    links, nxt = ([page], None) if direct else listing_links(html, page)
                    if not links:
                        raise ScraperError('В каталожната страница не са намерени продукти.')
                    for url in links:
                        if url in visited:
                            continue
                        if (args.limit and len(products) >= args.limit) or time.monotonic() >= deadline:
                            time_limit = time.monotonic() >= deadline
                            break
                        product = parse_product(html if direct else client.get(url), url, public=args.public)
                        stock = stock_state(product)
                        category, _ = classify(product, config, schema)
                        if stock == 'in_stock' and category not in schema.categories:
                            event = {'url': url, 'sku': product['sku'], 'reason': 'category_not_selected'}
                            skipped.append(event)
                        elif stock == 'in_stock' and product['sku'] in seen_skus:
                            event = {'url': url, 'sku': product['sku'], 'reason': 'duplicate_sku'}
                            skipped.append(event)
                        elif stock == 'in_stock':
                            event = {'url': url, 'product': product}
                            products.append(product)
                            seen_skus.add(product['sku'])
                            print(f"[{len(products)}] {product['sku']} | {product.get('price') or 'NO PRICE'} "
                                  f"{product.get('currency', '')} | {product['availability']}", flush=True)
                        else:
                            event = {'url': url, 'sku': product['sku'], 'reason': stock}
                            skipped.append(event)
                        checkpoint.write(json.dumps(event, ensure_ascii=False) + '\n')
                        checkpoint.flush()
                        visited.add(url)
                    if time_limit or (args.limit and len(products) >= args.limit):
                        break
                    page = nxt
                    save_state(index, page)
                if time_limit or (args.limit and len(products) >= args.limit):
                    break
                cursor = None
                save_state(index + 1, None)
            else:
                complete = True
    except KeyboardInterrupt:
        error = 'Interrupted; partial results saved'
    except (ScraperError, requests.RequestException) as exc:
        # Never dump requests, credentials, cookies, or server response HTML.
        error = str(exc) if isinstance(exc, ScraperError) else type(exc).__name__
        print(error, file=sys.stderr, flush=True)
    summary = make_exports(schema, config, products, args.output, skipped,
        {'public': args.public, 'crawl_complete': complete, 'time_limit_reached': time_limit,
         'product_limit': args.limit, 'pages_this_run': pages, 'crawl_error': error})
    if error:
        return 1
    if not args.public and summary['missing_price']:
        print('Има продукти без разпозната цена. Проверете validation_report.csv.', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ScraperError, ValueError, requests.RequestException) as exc:
        print(str(exc) if not isinstance(exc, requests.RequestException) else type(exc).__name__, file=sys.stderr)
        sys.exit(1)
