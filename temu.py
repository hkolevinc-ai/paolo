"""Read Temu's own category rules and write cells without rebuilding its workbook.

Only row cells in Template are changed. All other ZIP entries, validations,
formulas, names, and hidden sheets remain byte-for-byte unchanged.
"""
import copy
import json
import re
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile, ZIP_DEFLATED
from xml.sax.saxutils import escape

NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
TAG = "{" + NS["s"] + "}"


def colnum(col):
    n = 0
    for c in col:
        n = n * 26 + ord(c) - 64
    return n


def colname(n):
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def parts(expr, sep=","):
    result, start, depth, quoted = [], 0, 0, False
    for i, c in enumerate(expr):
        if c == '"':
            quoted = not quoted
        if not quoted:
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
            elif c == sep and depth == 0:
                result.append(expr[start:i].strip())
                start = i + 1
    return result + [expr[start:].strip()]


class Template:
    def __init__(self, path):
        self.path = Path(path)
        with ZipFile(path) as z:
            self.strings = []
            if "xl/sharedStrings.xml" in z.namelist():
                ss = ET.fromstring(z.read("xl/sharedStrings.xml"))
                self.strings = ["".join(n.itertext()) for si in ss for n in [si] if si.tag == TAG + "si"]
            workbook = ET.fromstring(z.read("xl/workbook.xml"))
            rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
            relationships = {r.attrib["Id"]: r.attrib["Target"] for r in rels}
            sheet_paths = {}
            for sheet in workbook.find("s:sheets", NS):
                rid = sheet.attrib['{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id']
                target = relationships[rid]
                sheet_paths[sheet.attrib["name"]] = target.lstrip("/") if target.startswith("/") else "xl/" + target
            self.template_path = sheet_paths["Template"]
            self.original_xml = z.read(self.template_path).decode("utf-8")
            root = ET.fromstring(self.original_xml)
            first_rows = {int(r.attrib["r"]): self.row(r) for r in root.find("s:sheetData", NS)
                          if int(r.attrib["r"]) <= 5}
            self.headers = first_rows[2]
            self.codes = first_rows[4]
            self.styles = {re.sub(r"\d", "", c.attrib["r"]): c.attrib.get("s", "0")
                           for r in root.find("s:sheetData", NS) if r.attrib["r"] == "5" for c in r}
            self.row5_attrs = next(r.attrib.copy() for r in root.find("s:sheetData", NS) if r.attrib["r"] == "5")
            self.dv = {}
            for dv in root.findall("s:dataValidations/s:dataValidation", NS):
                formula = dv.find("s:formula1", NS)
                if formula is not None and dv.attrib.get("type") == "list":
                    for rng in dv.attrib["sqref"].split():
                        self.dv[re.sub(r"\d", "", rng.split(":")[0])] = formula.text
            self.dropdown = {}
            self.named = {}
            for r in ET.fromstring(z.read(sheet_paths["Dropdown Lists"])).find("s:sheetData", NS):
                cells = self.row(r)
                if "A" in cells:
                    vals = [v for k, v in sorted(cells.items(), key=lambda kv: colnum(kv[0])) if colnum(k) >= 3]
                    self.dropdown[cells["A"]] = cells.get("B", "")
                    self.named[cells.get("B", "")] = vals
            self.categories = {}
            for r in ET.fromstring(z.read(sheet_paths["Browse Data"])).find("s:sheetData", NS):
                cells = self.row(r)
                if cells.get("A"):
                    self.categories[str(cells["A"])] = cells.get("B", "")
            self.rules = {}
            for r in ET.fromstring(z.read(sheet_paths["GoodsLevelMode"])).find("s:sheetData", NS):
                cells = self.row(r)
                if "A" in cells:
                    self.rules[cells.pop("A")] = cells
            self.conditions = {}
            for r in ET.fromstring(z.read(sheet_paths["Conditions List"])).find("s:sheetData", NS):
                cells = self.row(r)
                if cells.get("A"):
                    self.conditions[cells["A"]] = [v for k, v in cells.items() if colnum(k) >= 3]
        self.columns = defaultdict(list)
        for col, label in self.headers.items():
            self.columns[label].append(col)

    def row(self, element):
        result = {}
        for cell in element.findall("s:c", NS):
            col = re.sub(r"\d", "", cell.attrib["r"])
            value = cell.find("s:v", NS)
            if cell.attrib.get("t") == "s" and value is not None:
                result[col] = self.strings[int(value.text)]
            elif cell.attrib.get("t") == "inlineStr":
                result[col] = "".join(cell.find("s:is", NS).itertext())
            elif value is not None:
                result[col] = value.text or ""
        return result

    def evaluate(self, expr, values):
        expr = expr.strip().lstrip("=")
        concat = parts(expr, "&")
        if len(concat) > 1:
            return "".join(str(self.evaluate(p, values)) for p in concat)
        if expr.startswith('"') and expr.endswith('"'):
            return expr[1:-1].replace('""', '"')
        call = re.fullmatch(r"([A-Za-z]+)\((.*)\)", expr, re.S)
        if call:
            name, rest = call.groups()
            args = parts(rest)
            if name.upper() == "IFERROR":
                try:
                    return self.evaluate(args[0], values)
                except (KeyError, ValueError):
                    return self.evaluate(args[1], values)
            if name.upper() == "ROW":
                return 5
            if name.upper() == "INDEX":
                col = args[0].split(":")[0].replace("$", "")
                return values.get(col, "")
            if name.upper() == "VLOOKUP":
                return self.dropdown[str(self.evaluate(args[0], values))]
            if name.upper() == "INDIRECT":
                return self.named[str(self.evaluate(args[0], values))]
        if re.fullmatch(r"\d+", expr):
            return int(expr)
        raise ValueError("Unsupported validation expression: " + expr[:80])

    def allowed(self, col, values):
        if col not in self.dv:
            return None
        try:
            return list(self.evaluate(self.dv[col], values))
        except (KeyError, ValueError):
            return []

    def active(self, col, values):
        code = self.codes[col]
        triggers = self.conditions.get(code)
        if triggers is None:
            triggers = self.conditions.get(code.replace("t_8_Governance Property:2", "t_8_EU Responsible person"))
        if not triggers:
            return False
        if code.startswith("t_4_"):
            return values.get("PY") in triggers
        if "|" in code:
            parent = code.rsplit("|", 1)[-1].split(":")[-1]
            return any(values.get(c) in triggers for c, identity in self.codes.items()
                       if re.match(r"t_3_Property:" + re.escape(parent) + r"(?:$|\|)", identity))
        return any(v in triggers for v in values.values() if isinstance(v, str))

    def required_groups(self, category, values, manufacturer_is_eu=False):
        groups = defaultdict(list)
        required = set(self.rules.get(category + "_require", {}))
        conditional = self.rules.get(category + "_condition_require", {})
        required.update(c for c in conditional if self.active(c, values))
        # Instructions permit N/A instead of List Price, and EU manufacturers
        # need no separate EU Responsible Person. EU status must be explicit.
        if values.get("RC") == "N/A":
            required.discard("RB")
        if manufacturer_is_eu:
            required.discard("TW")
        for col in sorted(required, key=colnum):
            code = self.codes.get(col, col)
            composition = re.match(r'^(t_3_Property:(?:15|8652|8637)):', code)
            if composition:
                code = composition[1]
            groups[code].append(col)
        return list(groups.values())

    def validate(self, values, manufacturer_is_eu=False):
        errors = []
        category = str(values.get("E", ""))
        if category not in self.categories:
            return ["Няма подходяща категория в предоставения темплейт"]
        disabled = self.rules.get(category + "_disabled", {})
        for col, val in values.items():
            if val in ("", None):
                continue
            if col in disabled:
                errors.append(f"{col}: полето е забранено за категория {category}")
            allowed = self.allowed(col, values)
            if allowed is not None and str(val) not in [str(a) for a in allowed]:
                errors.append(f"{col} {self.headers[col]}: невалидна стойност {val!r} за падащото меню")
        for group in self.required_groups(category, values, manufacturer_is_eu):
            if not any(values.get(c) not in (None, "") for c in group):
                errors.append(f"{group[0]} {self.headers[group[0]]}: задължително поле")
        # Variation theme requires its selected axis even when not flagged in GoodsLevelMode.
        theme = values.get("PY", "")
        for axis in str(theme).split(" × "):
            cols = [c for c in self.columns.get(axis, []) if self.codes.get(c, "").startswith("t_4_")]
            if cols and not any(values.get(c) for c in cols):
                errors.append(f"Липсва избраната вариация {axis}")
        for col in ["QY", "QZ", "RD", "RE", "RF", "RG", "RJ"]:
            if values.get(col) not in (None, ""):
                try:
                    n = Decimal(str(values[col]))
                    if not n.is_finite() or n <= 0:
                        raise ValueError()
                    if col in ("QY", "RJ") and n != n.to_integral_value():
                        raise ValueError()
                    if col in ("RE", "RF", "RG") and (n > Decimal("9999.9") or n.as_tuple().exponent < -1):
                        raise ValueError()
                    if col in ("QZ", "RB") and n.as_tuple().exponent < -2:
                        raise ValueError()
                except (ValueError, ArithmeticError):
                    errors.append(f"{col} {self.headers[col]}: невалидно число")
        composition = defaultdict(list)
        for col, value in values.items():
            if value in (None, ''):
                continue
            m = re.match(r'^t_3_Property:(15|8652|8637):', self.codes.get(col, ''))
            if m:
                composition[m[1]].append(value)
        for identity, amounts in composition.items():
            try:
                numbers = [Decimal(str(a)) for a in amounts]
                if any(not n.is_finite() or n < 0 or n > 100 for n in numbers) or sum(numbers) != 100:
                    raise ValueError()
            except (ValueError, ArithmeticError):
                errors.append(f'Composition {identity}: процентите трябва да са числа от 0 до 100 с общ сбор 100')
        if values.get("RO") and not values.get("RP"):
            errors.append("RP External Product ID: липсва идентификатор")
        return list(dict.fromkeys(errors))

    def write(self, records, destination):
        if len(records) > 9996:
            raise ValueError("Един файл поддържа най-много 9996 реда; разделете го на части.")
        numeric = {"E", "QY", "QZ", "RB", "RD", "RE", "RF", "RG", "RJ", "RL", "RM"}
        # Replace cells/rows by text patches, so namespace prefixes and extensions
        # are not serialized differently by ElementTree.
        rows = []
        sample = re.search(r'<row\b[^>]*\br="5"[^>]*>.*?</row>', self.original_xml, re.S)
        if not sample:
            raise ValueError("Template row 5 is missing")
        sample_xml = sample.group(0)
        for index, values in enumerate(records, 5):
            base = re.sub(r'(<row\b[^>]*\br=")5(")', rf'\g<1>{index}\2', sample_xml, count=1)
            base = re.sub(r'(<c\b[^>]*\br="[A-Z]+)5(")', rf'\g<1>{index}\2', base)
            # Keep the native category-name formula and its cached category path.
            cached = escape(self.categories.get(str(values.get("E", "")), ""))
            base = re.sub(r'(<c\b[^>]*\br="F' + str(index) + r'"[^>]*>)(.*?)(</c>)',
                          lambda m: re.sub(r'\st="[^"]*"', '', m[1])[:-1] + ' t="str">'
                          + re.sub(r'<v>.*?</v>', '', m[2], flags=re.S) + '<v>' + cached + '</v></c>', base, flags=re.S)
            for col, value in sorted(values.items(), key=lambda kv: colnum(kv[0])):
                if value in (None, "") or col == "F":
                    continue
                ref = col + str(index)
                style = self.styles.get(col, "0")
                is_numeric = col in numeric or isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)
                if is_numeric:
                    try:
                        n = Decimal(str(value))
                        if not n.is_finite():
                            raise ValueError()
                        cell = f'<c r="{ref}" s="{style}"><v>{format(n, "f")}</v></c>'
                    except (ArithmeticError, ValueError):
                        is_numeric = False
                if not is_numeric:
                    clean = re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1F]', '', str(value))[:32767]
                    cell = f'<c r="{ref}" s="{style}" t="inlineStr"><is><t xml:space="preserve">{escape(clean)}</t></is></c>'
                pattern = r'<c\b[^>]*\br="' + ref + r'"[^>]*(?:/>|>.*?</c>)'
                base, count = re.subn(pattern, lambda _: cell, base, count=1, flags=re.S)
                if not count:
                    base = base.replace('</row>', cell + '</row>')
            # Excel requires ascending cell references.
            cells = re.findall(r'<c\b[^>]*(?:/>|>.*?</c>)', base, re.S)
            cells.sort(key=lambda c: colnum(re.search(r'\br="([A-Z]+)\d+"', c)[1]))
            base = re.sub(r'(<row\b[^>]*>).*?(</row>)', lambda m: m[1] + ''.join(cells) + m[2], base, flags=re.S)
            rows.append(base)
        xml = self.original_xml
        data = re.search(r'(<sheetData>)(.*?)(</sheetData>)', xml, re.S)
        if not data:
            raise ValueError("Template sheetData is missing")
        oldrows = re.findall(r'<row\b[^>]*(?:/>|>.*?</row>)\s*', data[2], re.S)
        preserved = [r for r in oldrows if int(re.search(r'\br="(\d+)"', r)[1]) < 5]
        xml = xml[:data.start(2)] + ''.join(preserved + rows) + xml[data.end(2):]
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        tmp = destination.with_suffix('.tmp.xlsx')
        with ZipFile(self.path) as src, ZipFile(tmp, 'w', compression=ZIP_DEFLATED) as out:
            for entry in src.infolist():
                out.writestr(entry, xml.encode('utf-8') if entry.filename == self.template_path else src.read(entry.filename))
        tmp.replace(destination)


MATERIALS = [
    (r'неръждаем|stainless', 'Stainless Steel'), (r'алумини|aluminium|aluminum', 'Aluminum'),
    (r'силикон|silicone', 'Silicone'), (r'гума|гумен|rubber', 'Rubber'),
    (r'пластмас|plastic', 'Plastic'), (r'полиестер|polyester', 'Polyester'),
    (r'полипропилен|polypropylene', 'Polypropylene'), (r'поликарбонат|polycarbonate', 'Polycarbonate'),
    (r'\babs\b', 'ABS plastic'), (r'\bpvc\b', 'Polyvinyl chloride'),
    (r'стоман|steel', 'Steel'), (r'стъкло|glass', 'Glass'), (r'медни|меден|copper', 'Copper'),
    (r'метал|metal', 'Metal'), (r'кожа|leather', 'Leather'), (r'памук|cotton', 'Cotton')]


def classify(product, config, schema):
    explicit = config.get('sku_categories', {}).get(str(product['sku']))
    if explicit:
        return str(explicit), 'sku_categories'
    for rule in config.get('category_rules', []):
        haystack = str(product.get(rule.get('field', 'category'), ''))
        if re.search(rule['pattern'], haystack, re.I):
            cat = str(rule['category'])
            if cat not in schema.categories:
                raise ValueError('Category rule is not in template: ' + cat)
            return cat, rule['pattern']
    return '', ''


def map_product(product, config, schema):
    category, matched = classify(product, config, schema)
    v = {'E': category, 'L': re.sub(r'\s+', ' ', product['name']).strip()[:500],
         'M': str(product['sku']), 'N': str(product['sku']), 'O': 'Add', 'G': 'Normal product',
         'T': product.get('description', '')[:10000], 'RA': product['url'], 'TU': str(product['sku'])}
    evidence = {'category_rule': matched, 'price_source': product.get('price_source', '')}
    warnings = []
    if product.get('currency') == 'EUR' and product.get('price'):
        v['QZ'] = product['price']
    else:
        warnings.append('Липсва потвърдена единична B2B цена в EUR')
    # No invented list price. The template explicitly allows N/A.
    v['RC'] = 'N/A'
    images = product.get('images', '').splitlines()
    for label, limit in [('SKU Images URL', 10), ('Detail Images URL', 96)]:
        for col, image in zip(schema.columns[label][:limit], images):
            v[col] = image
    base = config.get('defaults', {})
    brand = product.get('brand', '')
    overrides = {}
    for key, settings in config.get('brand_defaults', {}).items():
        if key.casefold() == str(brand).casefold():
            overrides.update(settings)
    overrides.update(config.get('category_defaults', {}).get(category, {}))
    overrides.update(config.get('sku_overrides', {}).get(str(product['sku']), {}))
    for col, val in {**base, **overrides}.items():
        if not col.startswith('_') and val not in ('', None):
            if col not in schema.headers:
                raise ValueError('Unknown override column: ' + col)
            v[col] = val
    evidence['configured_fields'] = [k for k in {**base, **overrides} if not k.startswith('_')]
    # Stock is a seller-selected Temu allocation, never a claimed warehouse count.
    allocation = int(config.get('quantity_if_in_stock', 1))
    if allocation < 1:
        raise ValueError('quantity_if_in_stock must be positive')
    v.setdefault('QY', allocation)
    evidence['quantity_source'] = 'configured Temu allocation; website in-stock status'
    # Supply a named single SKU axis using the actual source SKU.
    v.setdefault('PY', 'Model')
    v.setdefault('QL', str(product['sku']))
    source = (product.get('name', '') + '\n' + product.get('description', '')).lower()
    material_source = product.get('name', '').lower() + '\n' + '\n'.join(re.findall(
        r'(?:материал(?:и)?\s*:|изработен[аио]?\s+от|material\s*:)\s*([^\n.;]+)', source))
    disabled = schema.rules.get(category + '_disabled', {})
    for label in ['12 - Material', '1920 - Major Material', '121 - Material']:
        cols = [c for c in schema.columns[label] if c not in disabled]
        found = []
        for pattern, value in MATERIALS:
            if re.search(pattern, material_source):
                for col in cols:
                    allowed = schema.allowed(col, v)
                    if allowed and value in allowed and value not in found:
                        found.append(value)
                        break
        for col, value in zip(cols, found):
            v.setdefault(col, value)
    if 'DZ' not in disabled and source and 'See Product Details' in (schema.allowed('DZ', v) or []):
        v.setdefault('DZ', 'See Product Details')
    # Parse explicitly published packaged dimensions/weight only, not object size.
    for key, col in [('package_weight_g', 'RD'), ('package_length_cm', 'RE'),
                     ('package_width_cm', 'RF'), ('package_height_cm', 'RG')]:
        if product.get(key):
            v.setdefault(col, product[key])
    # Source structured attributes keyed by the exact template header are allowed
    # only if the value is in that column's own dependent dropdown.
    for label, value in product.get('attributes', {}).items():
        for col in schema.columns.get(label, []):
            allowed = schema.allowed(col, v)
            if col in disabled or not allowed:
                continue
            if value in allowed:
                v.setdefault(col, value)
                break
    if product.get('gtin') and str(product['gtin']).isdigit() and len(str(product['gtin'])) == 13:
        col_allowed = schema.allowed('RO', v)
        if col_allowed and 'EAN' in col_allowed:
            v.setdefault('RO', 'EAN')
            v.setdefault('RP', str(product['gtin']))
    if product.get('options_json'):
        warnings.append('Страницата има опции: изисква се потвърждение на SKU/цена/наличност за всяка вариация')
    eu = bool(overrides.get('_manufacturer_is_eu', base.get('_manufacturer_is_eu', False)))
    errors = warnings + schema.validate(v, eu)
    return v, list(dict.fromkeys(errors)), evidence
