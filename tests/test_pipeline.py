import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import requests

from paolo import BASE, Client, ScraperError, listing_links, parse_product, site_url, stock_state
from run import main
from run import make_exports
from enrichment import infer_fields
from temu import NS, TAG, Template, map_product

ROOT = Path(__file__).resolve().parents[1]


def fixture(status='Налично', price='12.34', guest=False):
    data = {'@type': 'Product', 'name': 'Крушки D2S', 'sku': '00123',
            'brand': {'name': 'Paolo'}}
    return f'''<script type="application/ld+json">{json.dumps(data)}</script>
    <div class="breadcrumbs"><a>Начало</a><a>Ксенонови крушки</a></div><h1>Крушки D2S</h1>
    <section id="products-app"><ul id="images"><img src="/Images/Items/Big/a.jpg"></ul>
    <div class="rside"><span class="sku">00123</span><span class="status">{status}</span>
    {'<div class="b2b-guest-cta">Вход</div>' if guest else ''}
    <input id="item_final_price" value="{price}"></div><div id="tab1">Описание</div></section>
    <div class="similar-products"><div class="price">999 €</div><span class="status">Изчерпан</span></div>'''


class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = Template(ROOT / 'templates/temu_template.xlsx')
        cls.config = json.loads((ROOT / 'config.json').read_text(encoding='utf-8'))

    def product(self):
        return parse_product(fixture(), BASE + 'pr-test')

    def test_stock_positive_and_negative(self):
        for value in ['Налично', 'В наличност']:
            self.assertEqual(stock_state({'availability': value}), 'in_stock')
        for value in ['На изчерпване', 'Последни бройки', 'Limited availability']:
            self.assertEqual(stock_state({'availability': value}), 'low_stock')
        for value in ['Изчерпан', 'Няма наличност', 'Не е наличен', 'По поръчка', 'Out of stock']:
            self.assertEqual(stock_state({'availability': value}), 'out_of_stock')
        self.assertEqual(stock_state({'availability': 'Налично', 'purchase_limit': '0'}), 'out_of_stock')
        self.assertEqual(stock_state({'availability': ''}), 'unknown')

    def test_real_price_excludes_related_product(self):
        p = self.product()
        self.assertEqual((p['price'], p['currency'], p['sku']), ('12.34', 'EUR', '00123'))
        self.assertEqual(p['availability'], 'Налично')
        self.assertEqual(p['images'], BASE + 'Images/Items/Big/a.jpg')

    def test_vehicle_light_specs_from_description(self):
        p = self.product()
        p['name'] = 'Диоден халоген 1брой'
        p['description'] = 'Работно напрежение: 12-24V DC. Свързване: Три проводника. LED осветление.'
        fields, evidence = infer_fields(p, {'E': '21978'}, self.template)
        self.assertEqual(fields['GA'], 'Car Plug')
        self.assertEqual(fields['GC'], '12-24V')
        self.assertEqual(fields['GD'], 'Without Battery')
        self.assertEqual(fields['RJ'], 1)
        self.assertEqual(fields['RK'], 'piece')
        self.assertEqual(fields['FY'], 'LED Lights')
        self.assertNotIn('RD', fields)

    def test_xenon_set_and_glass(self):
        p = self.product()
        p['name'] = 'Ксенонови крушки D2S 4300K 2 БР/К-Т'
        p['description'] = 'Цокъл: D2S. Изисква съвместим баласт. Комплект: 2 броя. Не докосвайте стъклената част.'
        fields, _ = infer_fields(p, {'E': '21922'}, self.template)
        self.assertEqual(fields['FK'], 'Glass')
        self.assertEqual(fields['GK'], '4300 Kelvin')
        self.assertEqual(fields['GH'], 'D2S/D2R')
        self.assertEqual(fields['RJ'], 2)
        self.assertNotIn('GC', fields)  # Ballast voltage was not published.

    def test_item_size_does_not_become_package_size(self):
        p = self.product()
        p['description'] = 'Ширина: 10.5 см. Височина: 12 см. Тегло: 100 g.'
        fields, _ = infer_fields(p, {'E': '21978'}, self.template)
        self.assertFalse(any(c in fields for c in ['RD', 'RE', 'RF', 'RG']))

    def test_explicit_package_measurements(self):
        p = self.product()
        p['description'] = 'Тегло на опаковката: 0.2 kg. Размери на опаковката: 100 x 80 x 60 mm.'
        fields, _ = infer_fields(p, {'E': '21978'}, self.template)
        self.assertEqual(fields['RD'], '200.0')
        self.assertEqual([fields[c] for c in ['RE', 'RF', 'RG']], ['10.0', '8.0', '6.0'])

    def test_brand_is_not_manufacturer_or_origin(self):
        fields, _ = infer_fields(self.product(), {'E': '21922'}, self.template)
        self.assertNotIn('TV', fields)
        self.assertNotIn('TW', fields)
        self.assertNotIn('RU', fields)

    def test_export_also_refilters_old_low_stock_checkpoint(self):
        p = self.product()
        p['availability'] = 'На изчерпване'
        with tempfile.TemporaryDirectory() as tmp:
            summary = make_exports(self.template, self.config, [p], Path(tmp), [], {})
            self.assertEqual(summary['in_stock_products'], 0)
            self.assertEqual(summary['excluded'], 1)
            self.assertNotIn(p['sku'], (Path(tmp) / 'products_with_prices.csv').read_text())
            self.assertFalse((Path(tmp) / 'temu_draft.xlsx').exists())

    def test_ambiguous_price_is_not_guessed(self):
        html = fixture(price='').replace('</div><div id="tab1">', '<div class="price">12 € / 100 € кашон</div></div><div id="tab1">')
        self.assertEqual(parse_product(html, BASE + 'pr-test')['price'], '')

    def test_expired_session_is_rejected(self):
        with self.assertRaises(ScraperError):
            parse_product(fixture(guest=True), BASE + 'pr-test')

    def test_external_urls_rejected(self):
        for url in ['https://evil.example/login', 'https://user:pass@b2b.paolo.ltd/login', 'http://b2b.paolo.ltd/login']:
            with self.assertRaises(ScraperError):
                site_url(url)

    def test_pagination_and_duplicate_links(self):
        html = '<section id="product-listing"><a href="javascript:;">Cart</a><a href="/pr-a">A</a><a href="/pr-a">A</a></section><link rel="next" href="/produkti?page=2">'
        self.assertEqual(listing_links(html, BASE + 'produkti'), ([BASE + 'pr-a'], BASE + 'produkti?page=2'))

    def test_csrf_login_form(self):
        form = '<form action="/login"><input type="hidden" name="_token" value="test-token"><input name="pass"></form>'
        reply = requests.Response()
        reply.url, reply.status_code, reply._content = BASE, 200, b'<a href="/logout">Exit</a>'
        c = Client(0)
        with patch.object(c, 'get', side_effect=[form, '<a href="/logout">Exit</a>']):
            with patch.object(c, 'request', return_value=reply) as req:
                c.login('test@example.com', 'test-only-password')
                self.assertEqual(req.call_args.kwargs['data']['_token'], 'test-token')
                self.assertEqual(req.call_args.kwargs['data']['mail'], 'test@example.com')

    def test_template_has_expected_schema(self):
        self.assertEqual(len(self.template.categories), 86)
        self.assertEqual(self.template.headers['QZ'], 'Base Price - EUR')
        self.assertEqual(self.template.headers['RA'], 'Reference Link')

    def test_complete_known_product_can_pass_validation(self):
        product = self.product()
        product['name'] = 'Моторно масло'
        cfg = copy.deepcopy(self.config)
        cfg['sku_categories'] = {'00123': '20416'}
        cfg['sku_overrides'] = {'00123': {'RD': 1100, 'RE': 20, 'RF': 10, 'RG': 10,
            'RI': 'Yes', 'RJ': 1, 'RK': 'piece', 'RU': 'Germany', 'TV': 'Paolo',
            '_manufacturer_is_eu': True}}
        values, errors, _ = map_product(product, cfg, self.template)
        self.assertEqual(errors, [])
        self.assertEqual(values['E'], '20416')

    def test_mapping_price_reference_and_missing_fields(self):
        row, issues, evidence = map_product(self.product(), self.config, self.template)
        self.assertEqual(row['E'], '21922')
        self.assertEqual(row['QZ'], '12.34')
        self.assertEqual(row['RA'], BASE + 'pr-test')
        self.assertEqual(row['QY'], 1)
        self.assertEqual(row['RC'], 'N/A')
        self.assertNotIn('RB', row)
        self.assertNotIn('TV', row)
        self.assertTrue(any('RD ' in i for i in issues))
        self.assertTrue(any('TV ' in i for i in issues))
        self.assertFalse(any('RB ' in i for i in issues))

    def test_dropdown_is_validated_per_category(self):
        row = {'E': '21922', 'TV': 'An unregistered manufacturer'}
        issues = self.template.validate(row)
        self.assertTrue(any('TV Manufacturer: невалидна' in i for i in issues))
        self.assertIn('Paolo', self.template.allowed('TV', row))
        self.assertEqual(self.template.allowed('RQ', row), ['Lazur'])

    def test_repeated_material_columns_form_one_requirement(self):
        v = {'E': '22137', 'DS': 'Metal'}
        groups = self.template.required_groups('22137', v)
        material_groups = [g for g in groups if 'DS' in g]
        self.assertEqual(len(material_groups), 1)
        self.assertEqual(material_groups[0], ['DS', 'DT', 'DU', 'DV', 'DW'])
        issues = self.template.validate(v)
        self.assertFalse(any('12 - Material: задължително' in i for i in issues))

    def test_conditional_voltage(self):
        v = {'E': '21922', 'GA': 'Without electricity'}
        self.assertFalse(any('GC' in g for g in self.template.required_groups('21922', v)))
        v['GA'] = 'Car Plug'
        self.assertTrue(any('GC' in g for g in self.template.required_groups('21922', v)))

    def test_eu_manufacturer_exception_requires_explicit_setting(self):
        v = {'E': '21922'}
        self.assertTrue(any('TW' in g for g in self.template.required_groups('21922', v)))
        self.assertFalse(any('TW' in g for g in self.template.required_groups('21922', v, True)))

    def test_composition_requires_a_total_of_100(self):
        v = {'E': '22093', 'EJ': 50}
        self.assertTrue(any('общ сбор 100' in i for i in self.template.validate(v)))
        v['EU'] = 50
        self.assertFalse(any('общ сбор 100' in i for i in self.template.validate(v)))

    def test_resume_keeps_previous_products_and_continues(self):
        listing = '<section id="product-listing"><a href="/pr-first">A</a><a href="/pr-second">B</a></section>'
        second = fixture().replace('00123', '00456')
        with tempfile.TemporaryDirectory() as tmp:
            with patch('sys.argv', ['run.py', '--public', '--limit', '1', '--output', tmp]), \
                 patch.object(Client, 'get', side_effect=[listing, fixture()]):
                self.assertEqual(main(), 0)
            with patch('sys.argv', ['run.py', '--public', '--limit', '0', '--resume', '--output', tmp]), \
                 patch.object(Client, 'get', side_effect=[listing, second]) as getter:
                self.assertEqual(main(), 0)
                self.assertEqual(getter.call_count, 2)
            summary = json.loads((Path(tmp) / 'summary.json').read_text())
            self.assertEqual(summary['in_stock_products'], 2)
            self.assertTrue(summary['crawl_complete'])

    def test_template_preserves_all_other_parts_and_reserved_rows(self):
        v = {'E': '21922', 'L': '=HYPERLINK("https://example.com")', 'M': '00123',
             'N': '00123', 'QZ': '12.34', 'RA': BASE + 'pr-test'}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'filled.xlsx'
            self.template.write([v], path)
            with ZipFile(path) as new, ZipFile(self.template.path) as original:
                self.assertIsNone(new.testzip())
                self.assertEqual(new.namelist(), original.namelist())
                for name in new.namelist():
                    if name != self.template.template_path:
                        self.assertEqual(new.read(name), original.read(name), name)
                a = ET.fromstring(original.read(self.template.template_path))
                b = ET.fromstring(new.read(self.template.template_path))
                before, after = a.find('s:sheetData', NS), b.find('s:sheetData', NS)
                for i in range(4):
                    self.assertEqual(ET.tostring(before[i]), ET.tostring(after[i]))
                cells = {c.attrib['r']: c for c in after[4]}
                self.assertEqual(cells['N5'].find('s:is/s:t', NS).text, '00123')
                self.assertEqual(cells['QZ5'].find('s:v', NS).text, '12.34')
                self.assertEqual(cells['L5'].attrib['t'], 'inlineStr')
                self.assertIsNotNone(cells['F5'].find('s:f', NS))
                self.assertEqual(cells['F5'].find('s:v', NS).text, self.template.categories['21922'])

    def test_full_mocked_crawl_excludes_unavailable_product(self):
        listing = '<section id="product-listing"><a href="/pr-out">Out</a><a href="/pr-in">In</a></section>'
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            argv = ['run.py', '--public', '--limit', '1', '--output', tmp]
            with patch('sys.argv', argv), patch.object(Client, 'get', side_effect=[listing, fixture('Изчерпан'), fixture()]):
                self.assertEqual(main(), 0)
            summary = json.loads((output / 'summary.json').read_text())
            self.assertEqual(summary['in_stock_products'], 1)
            self.assertEqual(summary['excluded'], 1)
            self.assertEqual(summary['ready'], 0)
            self.assertFalse((output / 'temu_ready.xlsx').exists())
            self.assertTrue((output / 'temu_draft.xlsx').exists())
            raw = (output / 'products_with_prices.csv').read_text(encoding='utf-8-sig')
            self.assertIn('/pr-in', raw)
            self.assertNotIn('/pr-out', raw)


if __name__ == '__main__':
    unittest.main()
