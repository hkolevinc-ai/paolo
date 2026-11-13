"""Extract supported template values from published specifications.

Package measurements are only taken from explicitly labelled package fields.
Origin and manufacturer are never inferred from the seller or brand.
"""
import re
from decimal import Decimal


def infer_fields(product, values, schema):
    title = re.sub(r'\s+', ' ', product.get('name', '')).strip()
    description = re.sub(r'\s+', ' ', product.get('description', '')).strip()
    source = title + ' ' + description
    lower = source.casefold()
    result, evidence = {}, {}
    category = str(values.get('E', ''))
    disabled = schema.rules.get(category + '_disabled', {})

    def put(col, value, reason):
        if col in disabled or value in ('', None):
            return False
        allowed = schema.allowed(col, {**values, **result})
        if allowed is not None and str(value) not in [str(a) for a in allowed]:
            return False
        result[col] = value
        evidence[col] = reason
        return True

    def match(pattern, text=source):
        return re.search(pattern, text, re.I)

    # Published units in a sales set, never the number of LEDs or stock count.
    qty = match(r'комплект(?:ът)?\s*(?:(?:от|включва)\s*)?[:\-]?\s*(\d+)\s*(?:бр\b|бр\.|брой|броя)')
    if not qty:
        qty = match(r'(?<!\d)(\d+)\s*(?:брой|броя|бр\.?\s*/\s*к[\-/]?т|бр\b)', title)
    if qty and 0 < int(qty[1]) <= 10000:
        count = int(qty[1])
        put('RJ', count, qty[0])
        put('RK', 'piece', qty[0])
        put('RH', 'Single set' if count == 1 else 'Multi-piece set', qty[0])
    if match(r'(?:индивидуалн\w*|отделн\w*)\s+(?:защитн\w*\s+)?опаковк'):
        put('RI', 'Yes', 'Описание: индивидуални/отделни опаковки')

    seats = match(r'\b(\d+)\s*(?:мест(?:а|ен|на|ни)|седалки|seats)\b')
    if seats:
        seat_options = {5: '5 seats（2Front Seats+3Rear Seats）', 6: '6 Seats',
                        7: '7 Seats', 8: '8 Seats', 9: '9 Seats'}
        if int(seats[1]) in seat_options:
            put('PZ', seat_options[int(seats[1])], seats[0])

    light = category in {'21920', '21922', '21927', '21946', '21951', '21955',
                         '21956', '21961', '21964', '21965', '21967', '21968',
                         '21978', '22007', '22028', '21226'}
    wired = bool(match(r'баласт|(?:три|два|\d+)\s+проводника|(?:12\s*[-–/]\s*24|12|24)\s*v\b|външно\s+захранване'))
    own_battery = bool(match(r'вграден\w*\s+(?:акумулатор|батери)|акумулаторн\w*\s+(?:лампа|халоген)|презарежда'))
    if light and wired and not own_battery:
        # Temu's automotive power branch. This is not a mains plug declaration.
        put('GA', 'Car Plug', 'Извод от описаната автомобилна електрическа система/баласт/проводници')
        put('GD', 'Without Battery', 'Външно автомобилно захранване; продуктът е лампа/крушка без собствена батерия')
    voltage = match(r'(?:работно\s+напрежение|захранващо\s+напрежение|захранване|напрежение)\s*[:\-]?\s*(\d+(?:[.,]\d+)?)\s*(?:[-–/]\s*(\d+(?:[.,]\d+)?))?\s*v\b')
    if not voltage:
        voltage = match(r'\b(\d+)\s*(?:[-–/]\s*(\d+))?\s*v\b', title)
    if voltage:
        a, b = voltage[1].replace(',', '.'), voltage[2]
        actual = a + ('-' + b.replace(',', '.') if b else '') + 'V'
        if not put('GC', actual, voltage[0]):
            top = Decimal(b.replace(',', '.') if b else a)
            if top <= 36:
                put('GC', '≤36V', 'Диапазонът в менюто включва точно посоченото напрежение: ' + voltage[0])
    if light:
        if match(r'ксенон|\bxenon\b|\bhid\b'):
            put('FY', 'Xenon Lights', 'Описание: ксенонова/HID технология')
        elif match(r'\bled\b|диоден'):
            put('FY', 'LED Lights', 'Описание: LED/диодно осветление')
        elif match(r'халогенна?\s+крушк|halogen\s+bulb'):
            put('FY', 'Tungsten Halogen Lamp', 'Описание: халогенна крушка')
        temperature = match(r'\b(\d{4,5})\s*[kк]\b')
        if temperature:
            put('GK', f'{temperature[1]} Kelvin', temperature[0])
        socket = match(r'(?:цокъл|\bтип\b)\s*[:\-]?\s*(D[1-8][SR]|H\d{1,2}|T\d{1,2})\b')
        if socket:
            code = socket[1].upper()
            choices = schema.allowed('GH', {**values, **result}) or []
            option = next((c for c in choices if code in str(c).split('/')), '')
            if option:
                put('GH', option, socket[0])
        if match(r'стъклен\w*\s+част|стъклен\w*\s+(?:колба|тръба)|glass\s+(?:bulb|tube)'):
            for col in ['FK', 'DS', 'GR']:
                if put(col, 'Glass', 'Описание: стъклена част на крушката'):
                    break

    weight = match(r'(?:тегло\s+на\s+(?:опаковката|пакета|опакования\s+продукт)|бруто\s+тегло|пакетно\s+тегло|package\s+weight)\s*[:\-]?\s*(\d+(?:[.,]\d+)?)\s*(кг|kg|гр\.?|g)\b')
    if weight:
        amount = Decimal(weight[1].replace(',', '.'))
        if weight[2].casefold() in ('кг', 'kg'):
            amount *= 1000
        if amount > 0:
            put('RD', str(amount), weight[0])
    size = match(r'(?:размери?\s+на\s+(?:опаковката|пакета)|package\s+dimensions)\s*[:\-]?\s*(\d+(?:[.,]\d+)?)\s*[xх×]\s*(\d+(?:[.,]\d+)?)\s*[xх×]\s*(\d+(?:[.,]\d+)?)\s*(см|cm|мм|mm)\b')
    if size:
        divisor = 10 if size[4].casefold() in ('мм', 'mm') else 1
        from decimal import ROUND_CEILING
        for col, amount in zip(['RE', 'RF', 'RG'], size.groups()[:3]):
            n = (Decimal(amount.replace(',', '.')) / divisor).quantize(Decimal('0.1'), rounding=ROUND_CEILING)
            if n > 0:
                put(col, str(n), size[0])

    origins = {'китай': 'Mainland China', 'china': 'Mainland China', 'българия': 'Bulgaria',
               'bulgaria': 'Bulgaria', 'германия': 'Germany', 'germany': 'Germany',
               'италия': 'Italy', 'italy': 'Italy', 'полша': 'Poland', 'poland': 'Poland',
               'франция': 'France', 'france': 'France', 'турция': 'Türkiye', 'turkey': 'Türkiye'}
    origin = match(r'(?:страна\s+на\s+произход|произведен[аои]?\s+в|made\s+in|country\s+of\s+origin)\s*[:\-]?\s*([\wÀ-ž]+)')
    if origin and origin[1].casefold() in origins:
        put('RU', origins[origin[1].casefold()], origin[0])
    manufacturer = match(r'(?:производител|manufacturer)\s*:\s*([^\n;]+)', product.get('description', ''))
    if manufacturer:
        candidates = schema.allowed('TV', {**values, **result}) or []
        normalized = manufacturer[1].strip().casefold().rstrip('.')
        selected = next((c for c in candidates if str(c).casefold().rstrip('.') == normalized), '')
        if selected:
            put('TV', selected, manufacturer[0])
    return result, evidence
