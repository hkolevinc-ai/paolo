#!/usr/bin/env python3
"""Paolo B2B catalogue export. Only the login form uses POST; catalogue reads use GET."""
import argparse
import csv
import getpass
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE = "https://b2b.paolo.ltd/"
HEADERS = ["sku", "name", "category", "brand", "gtin", "mpn", "price", "currency",
           "price_source", "price_raw", "availability", "purchase_limit", "units_per_carton",
           "description", "images", "options_json", "url", "fetched_at", "review"]


class ScraperError(Exception):
    pass


def text(node):
    return " ".join(node.stripped_strings) if node else ""


def description_text(node):
    if not node:
        return ''
    blocks = []
    for block in node.select('p, li, h1, h2, h3, h4, tr'):
        if any(parent.name in ('p', 'li', 'tr') for parent in block.parents if parent is not node):
            continue
        content = text(block)
        if content:
            blocks.append(content)
    return '\n'.join(blocks) if blocks else text(node)


def site_url(value):
    url = urljoin(BASE, value)
    u = urlsplit(url)
    if (u.scheme != "https" or u.hostname != "b2b.paolo.ltd" or u.port not in (None, 443)
            or u.username or u.password):
        raise ScraperError("Разрешени са само HTTPS адреси от b2b.paolo.ltd.")
    return url.split("#")[0]


def number(value):
    raw = re.sub(r"\s+", "", str(value)).replace(",", ".")
    if not re.fullmatch(r"\d+(?:\.\d+)?", raw):
        return ""
    try:
        return format(Decimal(raw), "f")
    except InvalidOperation:
        return ""


def input_value(root, selector):
    el = root.select_one(selector)
    return str(el.get("value", "")) if el else ""


def product_schema(soup):
    def walk(node):
        if isinstance(node, list):
            for child in node:
                yield from walk(child)
        elif isinstance(node, dict):
            typ = node.get("@type", [])
            if "Product" in (typ if isinstance(typ, list) else [typ]):
                yield node
            if "@graph" in node:
                yield from walk(node["@graph"])
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            for product in walk(json.loads(script.string or script.get_text())):
                return product
        except (ValueError, TypeError):
            continue
    return {}


def price_data(root, schema, soup):
    # IDs are used by Paolo's product JavaScript. Do not use related-product cards,
    # crossed-out prices, instalments, or quantity thresholds as the unit price.
    raw_root = root.select_one('.rside') or root
    raw = " | ".join(dict.fromkeys(text(n) for n in raw_root.select(
        '#p1, .price, [itemprop="price"], .action-single-price') if text(n)))
    hidden = number(input_value(root, "#item_final_price"))
    p1 = root.select_one("#p1")
    matches = re.findall(r"(?<![\d.,])(\d+(?:[ \u00a0]\d{3})*(?:[.,]\d+)?)\s*(€|EUR|лв\.?|BGN)", text(p1), re.I)
    if hidden:
        # The site's JavaScript displays #item_final_price as EUR; verified in app.js.
        return hidden, "EUR", "#item_final_price", raw
    if len(matches) == 1:
        amount, unit = matches[0]
        return number(amount), "EUR" if unit.upper() in ("€", "EUR") else "BGN", "#p1", raw
    offers = schema.get("offers", {})
    if isinstance(offers, dict) and offers.get("price") is not None and offers.get("priceCurrency"):
        amount = number(offers["price"])
        if amount:
            return amount, str(offers["priceCurrency"]), "JSON-LD offers.price", raw
    # Preserve raw price text for review if the authenticated layout differs.
    return "", "", "", raw


def parse_product(html, url, public=False):
    soup = BeautifulSoup(html, "html.parser")
    root = soup.select_one("#products-app")
    if root is None:
        raise ScraperError("Липсва продуктовият блок; възможна промяна на сайта или изтекла сесия.")
    if not public and root.select_one(".b2b-guest-cta, .b2b-guest-price"):
        raise ScraperError("Цените са заключени. Сесията е изтекла или профилът няма B2B достъп.")
    schema = product_schema(soup)
    rside = root.select_one(".rside") or root
    sku = text(rside.select_one('[itemprop="sku"], .sku')) or str(schema.get("sku", ""))
    name = text(soup.select_one("h1")) or schema.get("name", "")
    if not sku or not name:
        raise ScraperError("Липсва SKU или име; сканирането е спряно за проверка.")
    price, currency, source, raw = price_data(root, schema, soup)
    images = [urljoin(url, el.get("data-src") or el.get("src", ""))
              for el in root.select("#images img") if el.get("data-src") or el.get("src")]
    if not images:
        images = schema.get("image", [])
        images = [images] if isinstance(images, str) else images
    brand = schema.get("brand", {})
    if isinstance(brand, dict):
        brand = brand.get("name", "")
    brand = brand or text(rside.select_one(".brand strong"))
    description = root.select_one("#tab1")
    availability = text(rside.select_one(".status"))
    # This is the purchase limit used by the cart, not a guaranteed warehouse count.
    cap = input_value(root, "#item_max_quantity")
    carton = input_value(root, "#carton-per-box")
    options = []
    for select in root.select("select.types, select[name*='type'], select#color"):
        options.append({"name": select.get("name") or select.get("id") or "type",
                        "options": [{"label": text(o), "value": o.get("value", "")}
                                    for o in select.select("option[value]") if o.get("value")]})
    issues = []
    if not price:
        issues.append("Цената изисква вход" if public else "Не е разпозната единична цена — проверете price_raw")
    if not availability:
        issues.append("Не е разпознат статус за наличност")
    if options:
        issues.append("Има опции — необходима е проверка на вариациите и доплащанията")
    return dict(zip(HEADERS, [sku, name,
        " > ".join(text(a) for a in soup.select('.breadcrumbs a') if text(a) != "Начало"),
        brand, schema.get("gtin", schema.get("gtin13", "")), schema.get("mpn", ""),
        price, currency, source, raw, availability, cap, carton,
        description_text(description) if description else schema.get("description", ""),
        "\n".join(dict.fromkeys(images)), json.dumps(options, ensure_ascii=False) if options else "",
        url, datetime.now(timezone.utc).isoformat(), "; ".join(issues)]))


def stock_state(product):
    status = re.sub(r'\s+', ' ', product.get('availability', '')).strip().casefold()
    if re.search(r'няма|не\s+е|неналич|изчерпан|очаква|по\s+поръчка|out.of.stock|unavailable|not.available|backorder|pre.?order|discontinued', status):
        return 'out_of_stock'
    if re.search(r'на\s+изчерпване|последни\s+бройки|low\s+stock|limited\s+availability', status):
        return 'low_stock'
    cap = product.get('purchase_limit', '')
    if str(cap).strip() == '0':
        return 'out_of_stock'
    if status in ('налично', 'наличен', 'налични', 'в наличност', 'in stock', 'instock'):
        return 'in_stock'
    return 'unknown'


def listing_links(html, url):
    soup = BeautifulSoup(html, "html.parser")
    root = soup.select_one("#product-listing")
    if root is None:
        raise ScraperError("Липсва каталогът #product-listing.")
    links = []
    for a in root.select('a[href]'):
        candidate = urljoin(url, a["href"])
        if not urlsplit(candidate).path.startswith("/pr-"):
            continue
        u = site_url(candidate)
        if u not in links:
            links.append(u)
    nxt = soup.select_one('link[rel="next"], .pagination a.next')
    return links, site_url(urljoin(url, nxt["href"])) if nxt and nxt.get("href") else None


class Client:
    def __init__(self, delay):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "PaoloCatalogueExporter/1.0",
                                     "Accept-Language": "bg,en;q=0.8"})
        retries = Retry(total=3, backoff_factor=1.5, status_forcelist=[429, 500, 502, 503, 504],
                        allowed_methods=["GET"], respect_retry_after_header=True)
        self.session.mount("https://", HTTPAdapter(max_retries=retries))
        self.delay = delay
        self.last_request = 0

    def request(self, method, url, **kwargs):
        url = site_url(url)
        time.sleep(max(0, self.delay - (time.monotonic() - self.last_request)))
        # Follow only same-origin redirects; never POST credentials to another host.
        for _ in range(8):
            self.last_request = time.monotonic()
            response = self.session.request(method, url, timeout=(15, 60), allow_redirects=False, **kwargs)
            if response.status_code in (301, 302, 303, 307, 308) and "Location" in response.headers:
                url = site_url(urljoin(url, response.headers["Location"]))
                if response.status_code == 303 or (response.status_code in (301, 302) and method == "POST"):
                    method = "GET"
                    kwargs.pop("data", None)
                continue
            response.raise_for_status()
            response.encoding = "utf-8"
            return response
        raise ScraperError("Твърде много пренасочвания.")

    def get(self, url):
        return self.request("GET", url).text

    def login(self, email, password):
        soup = BeautifulSoup(self.get(BASE + "login"), "html.parser")
        form = soup.select_one('form input[name="pass"]')
        form = form.find_parent("form") if form else None
        if form is None:
            raise ScraperError("Формата за вход е променена.")
        payload = {n["name"]: n.get("value", "") for n in form.select('input[type="hidden"][name]')}
        if not payload.get("_token"):
            raise ScraperError("Не е открит CSRF token за вход.")
        payload.update({"mail": email, "pass": password})
        result = self.request("POST", site_url(form.get("action", "/login")), data=payload,
                              headers={"Referer": BASE + "login", "Origin": BASE.rstrip("/")})
        # No credentials or session cookies are written to logs, disk, or outputs.
        check = BeautifulSoup(result.text, "html.parser")
        if urlsplit(result.url).path in ("/login", "/register") or check.select_one('input[name="pass"]'):
            raise ScraperError("Входът не е потвърден. Проверете имейла, паролата и одобрението на профила.")
        home = BeautifulSoup(self.get(BASE), "html.parser")
        if home.select_one(".b2b-guest-price, .b2b-guest-cta"):
            raise ScraperError("След вход цените остават заключени. Проверете B2B достъпа на профила.")


