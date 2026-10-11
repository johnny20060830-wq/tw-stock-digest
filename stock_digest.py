#!/usr/bin/env python3
"""每日股票推播：抓台股／美股資料、算技術面與籌碼面基礎分，輸出 JSON 給 Claude 撰寫推播。

只用 Python 標準函式庫（雲端環境沒有 pandas）。
用法：
  python3 stock_digest.py --tw 2308,2454 --us GOOGL,MU [--date 2026-10-08] [--out /tmp/stock]
    → 產生 holdings.txt（持股健檢）、hot.txt（今日熱門）、data.json（原始資料）
  python3 stock_digest.py --push messages.json --topic <ntfy topic>
"""
import argparse, csv, datetime as dt, email.utils, html, http.cookiejar, io, json, os, re, statistics, sys, time
import urllib.error, urllib.parse, urllib.request
import xml.etree.ElementTree as ET

UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/120 Safari/537.36')
TPE = dt.timezone(dt.timedelta(hours=8))
ERRORS = []


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def fail(source, e):
    if '不在雲端環境允許的網域內' in str(e):
        msg = str(e)  # 同一個被擋的網域只記一次
    else:
        msg = f'{source}: {type(e).__name__}: {str(e)[:160]}'
    if msg not in ERRORS:
        ERRORS.append(msg)
    log(f'[ERR] {source}: {e}')


BLOCKED = set()


def fetch(url, data=None, headers=None, timeout=45, retries=2, opener=None):
    """網路被雲端環境擋下（proxy 403）的網站記下來，之後直接跳過，不再重試。"""
    host = urllib.parse.urlparse(url).hostname
    if host in BLOCKED:
        raise RuntimeError(f'{host} 不在雲端環境允許的網域內')
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=data, headers={'User-Agent': UA, **(headers or {})})
            with (opener or urllib.request.build_opener()).open(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.headers.get('x-deny-reason'):
                BLOCKED.add(host)
                raise RuntimeError(f'{host} 不在雲端環境允許的網域內') from e
            if e.code < 500 and e.code != 429:
                raise
            last = e
        except Exception as e:  # noqa: BLE001
            if 'Tunnel connection failed' in str(e):
                BLOCKED.add(host)
                raise RuntimeError(f'{host} 不在雲端環境允許的網域內') from e
            last = e
        if attempt < retries:
            time.sleep(2 + attempt * 4)
    raise last


def fetch_json(url, **kw):
    return json.loads(fetch(url, **kw).decode('utf-8-sig'))


def num(s):
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    s = re.sub(r'<[^>]+>', '', str(s)).replace(',', '').strip()
    try:
        return float(s)
    except ValueError:
        return None


def r2(x, n=2):
    return None if x is None else round(x, n)


def is_common(code):
    """上市櫃普通股（排除 ETF、權證、特別股、91xx 存託憑證）。"""
    return bool(re.fullmatch(r'[1-9]\d{3}', code)) and not code.startswith('91')


# ---------------------------------------------------------------- 台股全市場（官方）

def twse_day(d):
    """上市每日收盤行情。回傳 None 代表當天沒有資料（休市或尚未公布）。"""
    j = fetch_json('https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date='
                   f'{d:%Y%m%d}&type=ALLBUT0999&response=json')
    if j.get('stat') != 'OK' or j.get('date') != f'{d:%Y%m%d}':
        return None
    out = {'stocks': {}, 'sectors': [], 'summary': {}}
    for t in j.get('tables', []):
        f, rows = t.get('fields') or [], t.get('data') or []
        title = t.get('title') or ''
        if '證券代號' in f and '收盤價' in f:
            ix = {k: f.index(k) for k in f}
            for r in rows:
                code = r[ix['證券代號']].strip()
                sign = re.sub(r'<[^>]+>', '', r[ix['漲跌(+/-)']]).strip()
                diff = num(r[ix['漲跌價差']])
                chg = None if sign == 'X' or diff is None else (-diff if sign == '-' else diff)
                out['stocks'][code] = {
                    'name': r[ix['證券名稱']].strip().rstrip('*'), 'market': '上市',
                    'open': num(r[ix['開盤價']]), 'high': num(r[ix['最高價']]),
                    'low': num(r[ix['最低價']]), 'close': num(r[ix['收盤價']]), 'chg': chg,
                    'vol': num(r[ix['成交股數']]) or 0, 'value': num(r[ix['成交金額']]) or 0}
        elif title.endswith('價格指數(臺灣證券交易所)'):
            skip = ('水泥窯製', '塑膠化工', '機電類', '化學生技醫療', '電子工業', '槓桿', '反向')
            for r in rows:
                pct = num(r[4])
                if pct is not None and '-' in re.sub(r'<[^>]+>', '', r[2]):
                    pct = -abs(pct)
                if r[0] == '發行量加權股價指數':
                    out['summary']['taiex'] = {'close': num(r[1]), 'pct': pct, 'pts': num(r[3])}
                elif r[0].endswith('類指數') and not any(s in r[0] for s in skip):
                    out['sectors'].append({'name': r[0].replace('類指數', ''), 'pct': pct})
        elif title == '漲跌證券數合計':
            for r in rows:
                if r[0].startswith('上漲'):
                    out['summary']['up'] = r[2]
                elif r[0].startswith('下跌'):
                    out['summary']['down'] = r[2]
        elif title.endswith('大盤統計資訊'):
            for r in rows:
                if r[0].startswith('1.一般股票'):
                    out['summary']['value_100m'] = r2((num(r[1]) or 0) / 1e8, 0)
    return out if out['stocks'] else None


def tpex_day(d):
    j = fetch_json('https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes?date='
                   f'{d:%Y}%2F{d:%m}%2F{d:%d}&id=&response=json')
    if j.get('date') != f'{d:%Y%m%d}':
        return None
    out = {'stocks': {}}
    for t in j.get('tables', []):
        f = t.get('fields') or []
        if '代號' not in f or '收盤' not in f:
            continue
        ix = {k: f.index(k) for k in f}
        for r in t.get('data') or []:
            code = r[ix['代號']].strip()
            out['stocks'][code] = {
                'name': r[ix['名稱']].strip().rstrip('*'), 'market': '上櫃',
                'open': num(r[ix['開盤']]), 'high': num(r[ix['最高']]), 'low': num(r[ix['最低']]),
                'close': num(r[ix['收盤']]), 'chg': num(r[ix['漲跌']]),
                'vol': num(r[ix['成交股數']]) or 0, 'value': num(r[ix['成交金額(元)']]) or 0,
                'shares': num(r[ix['發行股數']]) if '發行股數' in ix else None}
    return out if out['stocks'] else None


def twse_inst(d):
    j = fetch_json('https://www.twse.com.tw/rwd/zh/fund/T86?date='
                   f'{d:%Y%m%d}&selectType=ALLBUT0999&response=json')
    if j.get('stat') != 'OK' or j.get('date') != f'{d:%Y%m%d}':
        return None
    out = {}
    for r in j['data']:
        out[r[0].strip()] = {'f': (num(r[4]) or 0) + (num(r[7]) or 0), 't': num(r[10]) or 0,
                             'd': num(r[11]) or 0, 'all': num(r[18]) or 0}
    return out


def tpex_inst(d):
    j = fetch_json('https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade?type=Daily&sect=EW&date='
                   f'{d:%Y}%2F{d:%m}%2F{d:%d}&response=json')
    tables = [t for t in j.get('tables', []) if t.get('data')]
    roc = f'{d.year - 1911}/{d:%m}/{d:%d}'
    if not tables or tables[0].get('date') != roc:
        return None
    out = {}
    for r in tables[0]['data']:
        out[r[0].strip()] = {'f': num(r[10]) or 0, 't': num(r[13]) or 0,
                             'd': num(r[22]) or 0, 'all': num(r[23]) or 0}
    return out


def twse_shares():
    out = {}
    for r in fetch_json('https://openapi.twse.com.tw/v1/opendata/t187ap03_L'):
        out[r['公司代號'].strip()] = num(r.get('已發行普通股數或TDR原股發行股數'))
    return out


def roc_date(s):
    """'115/10/08'、'1151008' → date。"""
    d = re.sub(r'\D', '', s)
    return dt.date(int(d[:-4]) + 1911, int(d[-4:-2]), int(d[-2:]))


def disposals(today):
    """處置中的股票：{代號: '10/08～10/15'}（人工管制撮合、需預收款券）。"""
    out = {}
    rows = [(r.get('Code', ''), r.get('DispositionPeriod', '')) for r in
            fetch_json('https://openapi.twse.com.tw/v1/announcement/punish')]
    try:
        rows += [(r.get('SecuritiesCompanyCode', ''), r.get('DispositionPeriod', '')) for r in
                 fetch_json('https://www.tpex.org.tw/openapi/v1/tpex_disposal_information')]
    except Exception as e:  # noqa: BLE001
        fail('上櫃處置股', e)
    for code, period in rows:
        parts = re.split(r'[～~]', period)
        if len(parts) != 2 or not is_common(code.strip()):
            continue
        try:
            start, end = roc_date(parts[0]), roc_date(parts[1])
        except (ValueError, IndexError):
            continue
        if end >= today:
            out[code.strip()] = f'{start:%m/%d}～{end:%m/%d}' + ('（即將開始）' if start > today else '')
    return out


# ---------------------------------------------------------------- FinMind（個股歷史、新聞、產業）

def finmind(dataset, data_id=None, start=None, end=None):
    q = {'dataset': dataset}
    if data_id:
        q['data_id'] = data_id
    if start:
        q['start_date'] = f'{start:%Y-%m-%d}'
    if end:
        q['end_date'] = f'{end:%Y-%m-%d}'
    j = fetch_json('https://api.finmindtrade.com/api/v4/data?' + urllib.parse.urlencode(q))
    if j.get('status') != 200:
        raise RuntimeError(j.get('msg'))
    return j['data']


GENERIC = {'電子工業', '所有證券', 'Index', '大盤', 'ETF', '上櫃ETF', '上櫃指數股票型基金(ETF)', 'ETN',
           '指數投資證券(ETN)', '存託憑證', '受益證券', '創新板股票', '創新版股票'}


_INFO = []


def stock_info():
    if not _INFO:
        _INFO.extend(finmind('TaiwanStockInfo'))
    return _INFO


def industries():
    out = {}
    for x in stock_info():
        cat = x['industry_category'].rstrip('類')
        if cat not in GENERIC and x['stock_id'] not in out:
            out[x['stock_id']] = cat
    return out


def names():
    """{代號: (名稱, 上市/上櫃)}，休市日沒有當天行情時用來補名稱。"""
    return {x['stock_id']: (x['stock_name'], {'twse': '上市', 'tpex': '上櫃'}.get(x['type'], ''))
            for x in stock_info()}


# ---------------------------------------------------------------- 集保股權分散（大戶／散戶）

def tdcc_levels_summary(levels):
    """levels: {分級: (人數, 股數, 比例%)}。大戶＝400張以上(12–15)，千張＝15，散戶＝10張以下(1–3)。"""
    pct = lambda ks: r2(sum(levels.get(k, (0, 0, 0))[2] for k in ks))
    people = levels.get(17, (None,))[0] or sum(levels.get(k, (0,))[0] for k in range(1, 16))
    return {'big400': pct(range(12, 16)), 'big1000': pct([15]), 'small10': pct([1, 2, 3]),
            'holders': people}


def tdcc_latest(codes):
    raw = fetch('https://opendata.tdcc.com.tw/getOD.ashx?id=1-5', timeout=90).decode('utf-8-sig')
    levels, date = {}, None
    for row in csv.reader(io.StringIO(raw)):
        if len(row) < 6 or row[1].strip() not in codes:
            continue
        date = row[0]
        levels.setdefault(row[1].strip(), {})[int(row[2])] = (num(row[3]) or 0, num(row[4]) or 0, num(row[5]) or 0)
    return date, {c: tdcc_levels_summary(lv) for c, lv in levels.items()}


class TdccHistory:
    URL = 'https://www.tdcc.com.tw/portal/zh/smWeb/qryStock'

    def __init__(self):
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        page = fetch(self.URL, opener=self.opener).decode('utf-8')
        self.dates = re.findall(r'<option value="(\d{8})"', page)
        self._read_tokens(page)

    def _read_tokens(self, page):
        m = re.search(r'name="SYNCHRONIZER_TOKEN" value="([^"]+)"', page)
        self.token = m.group(1) if m else ''
        m = re.search(r'name="firDate"[^>]*value="(\d+)"', page)
        self.fir = m.group(1) if m else (self.dates[0] if self.dates else '')

    def query(self, code, date):
        body = urllib.parse.urlencode({
            'SYNCHRONIZER_TOKEN': self.token, 'SYNCHRONIZER_URI': '/portal/zh/smWeb/qryStock',
            'method': 'submit', 'firDate': self.fir, 'scaDate': date, 'sqlMethod': 'StockNo',
            'stockNo': code, 'stockName': ''}).encode()
        page = fetch(self.URL, data=body, opener=self.opener,
                     headers={'Referer': self.URL, 'Content-Type': 'application/x-www-form-urlencoded'}).decode('utf-8')
        self._read_tokens(page)
        levels = {}
        for row in re.findall(r'<tr>(.*?)</tr>', page, re.S):
            cells = [html.unescape(re.sub(r'<[^>]+>', '', c)).strip() for c in re.findall(r'<td[^>]*>(.*?)</td>', row, re.S)]
            if len(cells) != 5:
                continue
            k = cells[0]
            lvl = int(k) if k.isdigit() else (16 if '差異' in k else 17 if '合' in k else None)
            if lvl:
                levels[lvl] = (num(cells[2]) or 0, num(cells[3]) or 0, num(cells[4]) or 0)
        return tdcc_levels_summary(levels) if levels else None


# ---------------------------------------------------------------- 技術指標

def sma(xs, n, end=None):
    xs = xs[:end] if end else xs
    return sum(xs[-n:]) / n if len(xs) >= n else None


def ema_list(xs, n):
    k, out, e = 2 / (n + 1), [], None
    for x in xs:
        e = x if e is None else e + k * (x - e)
        out.append(e)
    return out


def kd_list(highs, lows, closes, n=9):
    k = d = 50.0
    ks, ds = [], []
    for i in range(len(closes)):
        hi, lo = max(highs[max(0, i - n + 1):i + 1]), min(lows[max(0, i - n + 1):i + 1])
        rsv = 50 if hi == lo else (closes[i] - lo) / (hi - lo) * 100
        k = k * 2 / 3 + rsv / 3
        d = d * 2 / 3 + k / 3
        ks.append(k)
        ds.append(d)
    return ks, ds


def rsi_list(closes, n=14):
    out, ag, al = [None], None, None
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i - 1]
        g, l = max(ch, 0), max(-ch, 0)
        if i <= n:
            ag = (ag or 0) + g / n
            al = (al or 0) + l / n
            out.append(None if i < n else (100 if al == 0 else 100 - 100 / (1 + ag / al)))
        else:
            ag = (ag * (n - 1) + g) / n
            al = (al * (n - 1) + l) / n
            out.append(100 if al == 0 else 100 - 100 / (1 + ag / al))
    return out


def pivots(bars, w=4, lookback=90):
    """近期轉折點（波段高低點），讓 Claude 判讀 W 底、M 頭、頭肩、收斂等型態。"""
    seg = bars[-lookback:]
    out = []
    for i in range(w, len(seg) - w):
        win = seg[i - w:i + w + 1]
        if seg[i]['high'] == max(b['high'] for b in win):
            p = [seg[i]['date'][5:], 'H', seg[i]['high']]
        elif seg[i]['low'] == min(b['low'] for b in win):
            p = [seg[i]['date'][5:], 'L', seg[i]['low']]
        else:
            continue
        if out and out[-1][1] == p[1]:
            if (p[1] == 'H') == (p[2] >= out[-1][2]):
                out[-1] = p
            continue
        out.append(p)
    return out[-8:]


def tech(bars):
    """bars: [{'date','open','high','low','close','vol'}]（舊→新）。回傳指標、型態訊號、基礎分 0–10。"""
    if len(bars) < 30:
        return None
    c = [b['close'] for b in bars]
    h = [b['high'] for b in bars]
    lo = [b['low'] for b in bars]
    v = [b['vol'] for b in bars]
    close, prev = c[-1], c[-2]
    chg_pct = (close / prev - 1) * 100 if prev else 0
    ma = {n: sma(c, n) for n in (5, 10, 20, 60, 120, 240)}
    ma20_5ago = sma(c, 20, end=-5)
    dif = [a - b for a, b in zip(ema_list(c, 12), ema_list(c, 26))]
    dea = ema_list(dif, 9)
    osc = [a - b for a, b in zip(dif, dea)]
    ks, ds = kd_list(h, lo, c)
    rsi = rsi_list(c)[-1]
    sd = statistics.pstdev(c[-20:])
    bb_up, bb_lo = ma[20] + 2 * sd, ma[20] - 2 * sd
    avg5 = sum(v[-6:-1]) / 5
    avg20 = sum(v[-21:-1]) / 20 if len(v) > 21 else avg5
    vr5 = v[-1] / avg5 if avg5 else None
    hi20, lo20 = max(h[-21:-1]), min(lo[-21:-1])
    hi52, lo52 = max(h[-250:]), min(lo[-250:])

    sig, pts = [], 0.0
    # 1. 均線位置（3）
    pts += 0.5 * (close > ma[5]) + 0.5 * (close > ma[10]) + 1.0 * (close > ma[20])
    if ma[60]:
        pts += 1.0 * (close > ma[60])
    for n, label in ((5, '5日線'), (10, '10日線'), (20, '月線'), (60, '季線')):
        if ma[n] and prev is not None:
            pma = sma(c, n, end=-1)
            if pma and prev <= pma < close:
                sig.append(f'站上{label}')
            elif pma and prev >= pma > close:
                sig.append(f'跌破{label}')
    # 2. 均線排列與月線方向（2）
    if ma[60] and ma[5] > ma[10] > ma[20] > ma[60]:
        pts += 1.5
        sig.append('均線多頭排列')
    elif ma[5] > ma[10] > ma[20]:
        pts += 1.0
        sig.append('短均線多頭')
    elif ma[60] and ma[5] < ma[10] < ma[20] < ma[60]:
        sig.append('均線空頭排列')
    else:
        pts += 0.5
    if max(ma[5], ma[10], ma[20]) / min(ma[5], ma[10], ma[20]) < 1.02:
        sig.append('均線糾結')
    if ma20_5ago and ma[20] > ma20_5ago:
        pts += 0.5
    else:
        sig.append('月線下彎')
    # 3. MACD（1.5）
    pts += 0.5 * (dif[-1] > 0) + 0.5 * (osc[-1] > 0) + 0.5 * (osc[-1] > osc[-2])
    if osc[-2] <= 0 < osc[-1]:
        sig.append('MACD黃金交叉')
    elif osc[-2] >= 0 > osc[-1]:
        sig.append('MACD死亡交叉')
    # 4. KD（1）
    k, d = ks[-1], ds[-1]
    pts += 0.5 * (k > d)
    if 20 <= k <= 80 and k > ks[-2]:
        pts += 0.5
    elif k > 80:
        pts += 0.25
        sig.append('KD高檔')
    elif k < 20:
        sig.append('KD低檔')
    if ks[-2] <= ds[-2] and k > d:
        sig.append('KD黃金交叉' + ('(低檔)' if k < 30 else ''))
    elif ks[-2] >= ds[-2] and k < d:
        sig.append('KD死亡交叉' + ('(高檔)' if k > 70 else ''))
    # 5. RSI（0.5）
    if rsi is not None:
        if 50 <= rsi <= 75:
            pts += 0.5
        elif rsi > 80:
            sig.append('RSI過熱')
        elif rsi < 30:
            sig.append('RSI超賣')
    # 6. 量價（1.5）
    if vr5 is not None:
        if chg_pct > 0 and vr5 >= 1.2:
            pts += 1.5
            sig.append('價漲量增')
        elif chg_pct > 0:
            pts += 1.0
        elif vr5 < 0.8:
            pts += 0.75
            sig.append('量縮回檔')
        elif vr5 >= 1.5:
            sig.append('價跌量增')
        else:
            pts += 0.5
    # 7. 突破（0.5）
    if close > hi20:
        pts += 0.5
        sig.append('突破20日高' + ('・創52週新高' if close >= hi52 else ''))
    elif close < lo20:
        sig.append('跌破20日低')
    # K 線
    b = bars[-1]
    body = abs(b['close'] - b['open'])
    rng = b['high'] - b['low']
    if b['low'] > bars[-2]['high']:
        sig.append('向上跳空缺口')
    elif b['high'] < bars[-2]['low']:
        sig.append('向下跳空缺口')
    if b['open'] and body / b['open'] >= 0.04:
        sig.append('長紅K' if b['close'] > b['open'] else '長黑K')
    if rng and (b['high'] - max(b['open'], b['close'])) >= max(2 * body, 0.4 * rng) and rng / close > 0.02:
        sig.append('長上影線')
    if rng and (min(b['open'], b['close']) - b['low']) >= max(2 * body, 0.4 * rng) and rng / close > 0.02:
        sig.append('長下影線')
    streak = 0
    for i in range(len(c) - 1, 0, -1):
        s = 1 if c[i] > c[i - 1] else -1 if c[i] < c[i - 1] else 0
        if s == 0 or (streak and (s > 0) != (streak > 0)):
            break
        streak += s
    if abs(streak) >= 3:
        sig.append(f'連{abs(streak)}{"紅" if streak > 0 else "黑"}')
    bias20 = (close / ma[20] - 1) * 100
    if bias20 > 15:
        sig.append(f'正乖離過大({bias20:.0f}%)')
    elif bias20 < -12:
        sig.append(f'負乖離過大({bias20:.0f}%)')

    return {
        'score': max(0, min(10, round(pts))), 'raw': r2(pts), 'signals': sig,
        'close': close, 'chg_pct': r2(chg_pct),
        'ma': {str(n): r2(x) for n, x in ma.items() if x},
        'macd': {'dif': r2(dif[-1]), 'dea': r2(dea[-1]), 'osc': r2(osc[-1])},
        'kd': [r2(k, 1), r2(d, 1)], 'rsi14': r2(rsi, 1),
        'bb': {'up': r2(bb_up), 'low': r2(bb_lo), 'pct_b': r2((close - bb_lo) / (bb_up - bb_lo), 2) if bb_up > bb_lo else None,
               'width_pct': r2((bb_up - bb_lo) / ma[20] * 100, 1)},
        'bias20_pct': r2(bias20, 1), 'vol_ratio_5d': r2(vr5), 'vol_ratio_20d': r2(v[-1] / avg20 if avg20 else None),
        'ret_pct': {'5d': r2((close / c[-6] - 1) * 100) if len(c) > 5 else None,
                    '20d': r2((close / c[-21] - 1) * 100) if len(c) > 20 else None,
                    '60d': r2((close / c[-61] - 1) * 100) if len(c) > 60 else None},
        'high20': hi20, 'low20': lo20, 'high52w': hi52, 'low52w': lo52,
        'from_52w_high_pct': r2((close / hi52 - 1) * 100),
        'pivots': pivots(bars),
    }


# ---------------------------------------------------------------- 籌碼評分

def vol_points(chg_pct, vr):
    if vr is None:
        return None
    if chg_pct > 0:
        return 2 if vr >= 1.5 else 1.5 if vr >= 1 else 1
    return 1 if vr < 0.8 else 0.5 if vr < 1.5 else 0


def chips_tw(t, inst_days, tdcc, etf):
    """各項有資料才計分，最後依「有資料的滿分」換算成 0–10。"""
    got, full, notes = 0.0, 0.0, []
    vp = vol_points(t['chg_pct'], t['vol_ratio_5d'])
    if vp is not None:
        got += vp
        full += 2
    if inst_days:
        f0, t0 = inst_days[-1]['f'], inst_days[-1]['t']
        sum5 = sum(x['f'] + x['t'] for x in inst_days[-5:])
        sum20 = sum(x['f'] + x['t'] for x in inst_days[-20:])
        streak = {}
        for key in ('f', 't'):
            n = 0
            for x in reversed(inst_days):
                s = 1 if x[key] > 0 else -1 if x[key] < 0 else 0
                if s == 0 or (n and (s > 0) != (n > 0)):
                    break
                n += s
            streak[key] = n
        p = 0.5 * (f0 > 0) + 0.75 * (t0 > 0) + 0.75 * (sum5 > 0) + 0.5 * (sum20 > 0)
        p += 0.5 * (streak['f'] >= 3 or streak['t'] >= 3)
        got += min(p, 3)
        full += 3
        for key, label in (('f', '外資'), ('t', '投信')):
            if abs(streak[key]) >= 3:
                notes.append(f'{label}連{abs(streak[key])}{"買" if streak[key] > 0 else "賣"}')
    if tdcc and tdcc.get('d_big400_1w') is not None:
        db = (tdcc['d_big400_1w'] + tdcc.get('d_big1000_1w', tdcc['d_big400_1w'])) / 2
        ds = tdcc.get('d_small10_1w')
        p = 1.5 if db >= 0.5 else 1 if db > 0 else 0.5 if db > -0.5 else 0
        if ds is not None:
            p += 1 if ds < 0 else 0.5 if ds <= 0.3 else 0
        if tdcc.get('d_big400_4w') is not None and (tdcc['d_big400_4w'] + tdcc.get('d_big1000_4w', 0)) / 2 >= 1:
            p += 0.5
        got += min(p, 3)
        full += 3
    if etf is not None:
        p = 1 + min(1, 0.5 * etf['buy_events']) - min(1, 0.5 * etf['sell_events']) + 0.5 * etf['consensus_buy']
        got += max(0, min(2, p))
        full += 2
    score = round(got / full * 10) if full else None
    return {'score': score, 'raw': r2(got), 'full': full, 'notes': notes}


def chips_us(t, fv):
    got, full = 0.0, 0.0
    vp = vol_points(t['chg_pct'], t['vol_ratio_5d'])
    if vp is not None:
        got += vp * 1.5
        full += 3
    if fv:
        it, ins = num((fv.get('Inst Trans') or '').rstrip('%')), num((fv.get('Insider Trans') or '').rstrip('%'))
        if it is not None or ins is not None:
            p = 0
            if it is not None:
                p += 1.5 if it > 1 else 1 if it > 0 else 0.5 if it > -1 else 0
            if ins is not None:
                p += 1.5 if ins > 0 else 0.75 if ins >= -10 else 0
            got += p
            full += 3
        sf = num((fv.get('Short Float') or '').rstrip('%'))
        if sf is not None:
            got += 2 if sf < 3 else 1.5 if sf < 6 else 1 if sf < 10 else 0.5 if sf < 20 else 0
            full += 2
        rec, tgt, price = num(fv.get('Recom')), num(fv.get('Target Price')), t['close']
        if rec is not None or tgt is not None:
            p = 0
            if rec is not None:
                p += 1 if rec <= 1.5 else 0.75 if rec <= 2.2 else 0.25 if rec <= 3 else 0
            if tgt is not None and price:
                up = (tgt / price - 1) * 100
                p += 1 if up >= 20 else 0.75 if up >= 10 else 0.25 if up >= 0 else 0
            got += p
            full += 2
    return {'score': round(got / full * 10) if full else None, 'raw': r2(got), 'full': full}


# ---------------------------------------------------------------- 其他來源

def active_etf():
    return fetch_json('https://raw.githubusercontent.com/TANGCHIH0221/active-etf/main/active.json')


def etf_view(j, code, since):
    s = (j.get('stocks') or {}).get(code)
    if not s:
        return {'etf_count': 0, 'events': [], 'buy_events': 0, 'sell_events': 0, 'consensus_buy': False}
    ev = [e for e in s.get('recent_events', []) if e.get('date', '') >= since]
    name = {k: v.get('name', '') for k, v in (j.get('etfs') or {}).items()}
    zh = {'ADD': '新增', 'INCREASE': '加碼', 'DECREASE': '減碼', 'REMOVE': '剔除'}
    cons = {x['code'] for x in (j.get('consensus') or {}).get('increase', [])}
    return {
        'etf_count': s.get('etf_count'), 'total_value_100m': r2((s.get('total_value') or 0) / 1e8, 1),
        'events': [f"{e['date'][5:]} {e['etf']}{name.get(e['etf'], '')} {zh.get(e['type'], e['type'])}" for e in ev],
        'buy_events': sum(e['type'] in ('ADD', 'INCREASE') for e in ev),
        'sell_events': sum(e['type'] in ('DECREASE', 'REMOVE') for e in ev),
        'consensus_buy': code in cons}


def finviz(ticker):
    page = fetch(f'https://finviz.com/quote?t={ticker}&p=d').decode('utf-8', 'ignore')
    cells = re.findall(r'<td[^>]*class="snapshot-td2[^"]*"[^>]*>(.*?)</td>', page, re.S)
    vals = [html.unescape(re.sub(r'<[^>]+>', '', x)).strip() for x in cells]
    d = dict(zip(vals[0::2], vals[1::2]))
    keep = ('Short Float', 'Short Ratio', 'Inst Own', 'Inst Trans', 'Insider Own', 'Insider Trans', 'Recom',
            'Target Price', 'Rel Volume', 'Earnings', 'Market Cap', 'Forward P/E')
    return {k: d.get(k) for k in keep if d.get(k)}


JUNK = re.compile(r'個股概覽|爆料同學會|股市爆料|interactive|\b(put|call) \(|Stock Price Forecast|Prediction:')


def google_news(query, lang='zh-TW', max_items=6):
    hl, gl, ceid = ('zh-TW', 'TW', 'TW:zh-Hant') if lang == 'zh-TW' else ('en-US', 'US', 'US:en')
    url = ('https://news.google.com/rss/search?' +
           urllib.parse.urlencode({'q': f'{query} when:1d', 'hl': hl, 'gl': gl, 'ceid': ceid}))
    root = ET.fromstring(fetch(url))
    out = []
    for it in root.iter('item'):
        src = it.find('source')
        try:
            when = email.utils.parsedate_to_datetime(it.findtext('pubDate')).astimezone(TPE).strftime('%m-%d')
        except (TypeError, ValueError):
            when = ''
        out.append({'title': (it.findtext('title') or '').strip(), 'source': src.text if src is not None else '',
                    'time': when})
        if len(out) >= max_items:
            break
    return out


# ---------------------------------------------------------------- 主流程

def bars_from_finmind(rows, tw=True):
    out = []
    for x in rows:
        if tw:
            b = {'date': x['date'], 'open': x['open'], 'high': x['max'], 'low': x['min'], 'close': x['close'],
                 'vol': x['Trading_Volume']}
        else:
            b = {'date': x['date'], 'open': x['Open'], 'high': x['High'], 'low': x['Low'], 'close': x['Close'],
                 'vol': x['Volume']}
        if b['close'] and b['high'] and b['low']:
            out.append(b)
    return out


def run(args):
    now = dt.datetime.now(TPE)
    today = dt.date.fromisoformat(args.date) if args.date else now.date()
    tw_codes = list(dict.fromkeys(c.strip().upper() for c in args.tw.split(',') if c.strip()))
    us_codes = list(dict.fromkeys(c.strip().upper() for c in args.us.split(',') if c.strip()))
    out = {'meta': {'generated_at': now.strftime('%Y-%m-%d %H:%M'), 'today': str(today),
                    'weekday': '一二三四五六日'[today.weekday()]}}

    # --- 今日全市場
    tw_today = {}
    try:
        tw_day = twse_day(today)
    except Exception as e:  # noqa: BLE001
        tw_day = None
        fail('TWSE 今日行情', e)
    try:
        tp_day = tpex_day(today)
    except Exception as e:  # noqa: BLE001
        tp_day = None
        fail('TPEx 今日行情', e)
    out['meta']['tw_trading_day'] = bool(tw_day or tp_day)
    if tw_day:
        tw_today.update(tw_day['stocks'])
    if tp_day:
        tw_today.update(tp_day['stocks'])
    inst_today = {}
    if out['meta']['tw_trading_day']:
        for name, fn in (('TWSE 三大法人', twse_inst), ('TPEx 三大法人', tpex_inst)):
            try:
                got = fn(today)
                if got is None:
                    ERRORS.append(f'{name}: 今日資料尚未公布')
                else:
                    inst_today.update(got)
            except Exception as e:  # noqa: BLE001
                fail(name, e)
    out['meta']['tw_inst_ready'] = bool(inst_today)
    disp = {}
    try:
        disp = disposals(today)
    except Exception as e:  # noqa: BLE001
        fail('處置股名單', e)
    if tw_day:
        out['market'] = tw_day['summary']
        secs = sorted(tw_day['sectors'], key=lambda s: s['pct'] or 0, reverse=True)
        out['market']['sectors_top'] = secs[:6]
        out['market']['sectors_bottom'] = secs[-4:]

    # --- 集保與主動 ETF（台股持股才需要）
    tdcc_now, tdcc_date, tdcc_hist = {}, None, {}
    etf_json = None
    if tw_codes:
        try:
            tdcc_date, tdcc_now = tdcc_latest(set(tw_codes))
        except Exception as e:  # noqa: BLE001
            fail('集保股權分散(本週)', e)
        try:
            th = TdccHistory()
            cur = tdcc_date if tdcc_date in th.dates else (th.dates[0] if th.dates else None)
            i = th.dates.index(cur) if cur in th.dates else 0
            wanted = {'1w': th.dates[i + 1] if len(th.dates) > i + 1 else None,
                      '4w': th.dates[i + 4] if len(th.dates) > i + 4 else None}
            for code in tw_codes:
                tdcc_hist[code] = {}
                for key, d in wanted.items():
                    if d:
                        try:
                            tdcc_hist[code][key] = th.query(code, d)
                        except Exception as e:  # noqa: BLE001
                            fail(f'集保歷史 {code} {d}', e)
                        time.sleep(0.4)
            out['meta']['tdcc_dates'] = {'now': tdcc_date, **wanted}
        except Exception as e:  # noqa: BLE001
            fail('集保歷史', e)
        try:
            etf_json = active_etf()
            out['meta']['active_etf_date'] = etf_json.get('updated')
        except Exception as e:  # noqa: BLE001
            fail('主動式ETF持股', e)

    # --- 台股持股
    holdings = []
    for code in tw_codes:
        h = {'code': code}
        if code in disp:
            h['disposal'] = disp[code]
        try:
            rows = finmind('TaiwanStockPrice', code, today - dt.timedelta(days=420), today)
            bars = bars_from_finmind(rows)
            td = tw_today.get(code)
            if td and td.get('close') and (not bars or bars[-1]['date'] < str(today)):
                bars.append({'date': str(today), 'open': td['open'] or td['close'], 'high': td['high'] or td['close'],
                             'low': td['low'] or td['close'], 'close': td['close'], 'vol': td['vol']})
            if td:
                h['name'], h['market'] = td['name'], td['market']
            h['last_date'] = bars[-1]['date'] if bars else None
            t_now = tech(bars)
            t_prev = tech(bars[:-1]) if len(bars) > 31 else None
            if t_now:
                h['tech'] = t_now
                h['tech_score_yesterday'] = t_prev['score'] if t_prev else None
                h['recent_bars'] = [[b['date'][5:], b['open'], b['high'], b['low'], b['close'], round(b['vol'] / 1000)]
                                    for b in bars[-8:]]
        except Exception as e:  # noqa: BLE001
            fail(f'{code} 股價', e)
        inst_days = []
        try:
            rows = finmind('TaiwanStockInstitutionalInvestorsBuySell', code, today - dt.timedelta(days=45), today)
            byday = {}
            for x in rows:
                dd = byday.setdefault(x['date'], {'date': x['date'], 'f': 0, 't': 0, 'd': 0})
                net = (x['buy'] or 0) - (x['sell'] or 0)
                key = {'Foreign_Investor': 'f', 'Foreign_Dealer_Self': 'f', 'Investment_Trust': 't',
                       'Dealer_self': 'd', 'Dealer_Hedging': 'd'}.get(x['name'])
                if key:
                    dd[key] += net
            inst_days = [byday[k] for k in sorted(byday)]
        except Exception as e:  # noqa: BLE001
            fail(f'{code} 法人歷史', e)
        if code in inst_today and (not inst_days or inst_days[-1]['date'] < str(today)):
            x = inst_today[code]
            inst_days.append({'date': str(today), 'f': x['f'], 't': x['t'], 'd': x['d']})
        if inst_days and inst_days[-1]['date'] != str(today):
            inst_days_today = None
        else:
            inst_days_today = inst_days[-1] if inst_days else None
        lots = lambda s: round(s / 1000)
        if inst_days:
            h['inst'] = {
                'date': inst_days[-1]['date'],
                'today_lots': {k: lots(inst_days_today[k]) for k in ('f', 't', 'd')} if inst_days_today else None,
                'sum5_lots': {k: lots(sum(x[k] for x in inst_days[-5:])) for k in ('f', 't', 'd')},
                'sum20_lots': {k: lots(sum(x[k] for x in inst_days[-20:])) for k in ('f', 't', 'd')},
                'daily_ft_lots': [[x['date'][5:], lots(x['f']), lots(x['t'])] for x in inst_days[-6:]]}
            if inst_days_today and h.get('tech'):
                vol_lots = (tw_today.get(code) or {}).get('vol', 0) / 1000
                if vol_lots:
                    h['inst']['today_ft_pct_of_volume'] = r2((inst_days_today['f'] + inst_days_today['t']) / 1000 / vol_lots * 100, 1)
        td = None
        if code in tdcc_now:
            td = dict(tdcc_now[code])
            td['date'] = tdcc_date
            for key in ('1w', '4w'):
                old = (tdcc_hist.get(code) or {}).get(key)
                if old:
                    td[f'd_big400_{key}'] = r2(td['big400'] - old['big400'])
                    td[f'd_big1000_{key}'] = r2(td['big1000'] - old['big1000'])
                    td[f'd_small10_{key}'] = r2(td['small10'] - old['small10'])
                    if old.get('holders') and td.get('holders'):
                        td[f'd_holders_{key}'] = int(td['holders'] - old['holders'])
            h['tdcc'] = td
        etf = None
        if etf_json is not None:
            since = (dt.date.fromisoformat(etf_json.get('updated', str(today))) - dt.timedelta(days=4)).isoformat()
            etf = etf_view(etf_json, code, since)
            h['active_etf'] = {k: v for k, v in etf.items() if k not in ('buy_events', 'sell_events')}
        if h.get('tech'):
            h['chips'] = chips_tw(h['tech'], inst_days, td, etf)
            if len(inst_days) > 1:
                h['chips_score_yesterday'] = chips_tw(tech(bars[:-1]) or h['tech'], inst_days[:-1], td, etf)['score']
        holdings.append(h)
        time.sleep(0.2)
    if any('name' not in h for h in holdings):
        try:
            nm = names()
            for h in holdings:
                if 'name' not in h and h['code'] in nm:
                    h['name'], h['market'] = nm[h['code']]
        except Exception as e:  # noqa: BLE001
            fail('股票名稱', e)
    out['tw_holdings'] = holdings

    # --- 台股新聞
    for h in holdings:
        news = []
        for day in (today - dt.timedelta(days=1), today):
            try:
                for x in finmind('TaiwanStockNews', h['code'], day):
                    if x['date'] >= f'{today - dt.timedelta(days=1)} 06:00':
                        news.append({'title': x['title'], 'source': x.get('source'), 'time': x['date'][5:10]})
            except Exception as e:  # noqa: BLE001
                fail(f'{h["code"]} FinMind新聞 {day}', e)
        try:
            news += google_news(f'{h.get("name", "")} {h["code"]}')
        except Exception as e:  # noqa: BLE001
            fail(f'{h["code"]} Google新聞', e)
        seen, uniq = set(), []
        for n in news:
            key = re.sub(r'\W', '', n['title'])[:24]
            if JUNK.search(n['title']):
                continue
            if key not in seen:
                seen.add(key)
                uniq.append(n)
        h['news'] = uniq[:12]

    # --- 美股持股
    us = []
    for tk in us_codes:
        u = {'ticker': tk}
        try:
            rows = finmind('USStockPrice', tk, today - dt.timedelta(days=420), today - dt.timedelta(days=1))
            bars = bars_from_finmind(rows, tw=False)
            u['last_date'] = bars[-1]['date'] if bars else None
            t_now = tech(bars)
            if t_now:
                u['tech'] = t_now
                t_prev = tech(bars[:-1])
                u['tech_score_yesterday'] = t_prev['score'] if t_prev else None
                u['recent_bars'] = [[b['date'][5:], r2(b['open']), r2(b['high']), r2(b['low']), r2(b['close']),
                                     round(b['vol'] / 1e6, 1)] for b in bars[-8:]]
        except Exception as e:  # noqa: BLE001
            fail(f'{tk} 股價', e)
        fv = None
        try:
            fv = finviz(tk)
            u['finviz'] = fv
        except Exception as e:  # noqa: BLE001
            fail(f'{tk} finviz', e)
        if u.get('tech'):
            u['chips'] = chips_us(u['tech'], fv)
        try:
            seen, u['news'] = set(), []
            for n in google_news(f'{tk} stock', lang='en', max_items=12):
                key = re.sub(r'\W', '', n['title'])[:30]
                if not JUNK.search(n['title']) and key not in seen:
                    seen.add(key)
                    u['news'].append(n)
            u['news'] = u['news'][:6]
        except Exception as e:  # noqa: BLE001
            fail(f'{tk} Google新聞', e)
        us.append(u)
    out['us_holdings'] = us

    # --- 今日熱門（全市場）
    if out['meta']['tw_trading_day'] and not args.skip_hot:
        try:
            out['hot'] = hot_stocks(today, tw_today, inst_today, tw_codes, disp)
        except Exception as e:  # noqa: BLE001
            fail('今日熱門', e)

    out['errors'] = ERRORS
    return out


def hot_stocks(today, tw_today, inst_today, tw_codes, disp):
    # 前 5 個交易日的成交量（算 5 日均量）
    vols, d, days, tries = {}, today, [], 0
    while len(days) < 5 and tries < 14:
        d -= dt.timedelta(days=1)
        tries += 1
        if d.weekday() >= 5:
            continue
        got = {}
        try:
            a = twse_day(d)
            time.sleep(2.5)
            b = tpex_day(d)
        except Exception as e:  # noqa: BLE001
            fail(f'歷史行情 {d}', e)
            continue
        for src in (a, b):
            if src:
                got.update({k: v['vol'] for k, v in src['stocks'].items() if is_common(k)})
        if got:
            days.append(str(d))
            for k, v in got.items():
                vols.setdefault(k, []).append(v)
    try:
        ind = industries()
    except Exception as e:  # noqa: BLE001
        ind = {}
        fail('產業分類', e)
    shares = {}
    try:
        shares = twse_shares()
    except Exception as e:  # noqa: BLE001
        fail('上市股本', e)
    rows = []
    for code, s in tw_today.items():
        if not is_common(code) or not s.get('close') or s.get('chg') is None:
            continue
        prev = s['close'] - s['chg']
        if prev <= 0:
            continue
        hist = vols.get(code, [])
        avg5 = sum(hist) / len(hist) if len(hist) >= 3 else None
        sh = s.get('shares') or shares.get(code)
        inst = inst_today.get(code)
        rows.append({
            'code': code, 'name': s['name'], 'market': s['market'], 'industry': ind.get(code, ''),
            'close': s['close'], 'chg_pct': r2(s['chg'] / prev * 100), 'value_100m': r2(s['value'] / 1e8, 2),
            'vol_lots': round(s['vol'] / 1000), 'vol_ratio_5d': r2(s['vol'] / avg5) if avg5 else None,
            'capital_100m': r2(sh * 10 / 1e8, 1) if sh else None,
            'inst_lots': {k: round(inst[k] / 1000) for k in ('f', 't', 'd', 'all')} if inst else None,
            'mine': code in tw_codes, 'disposal': disp.get(code)})
    by = {r['code']: r for r in rows}

    gainers = sorted([r for r in rows if r['chg_pct'] >= 7], key=lambda r: -r['value_100m'])
    for r in gainers:
        r['limit_up'] = r['chg_pct'] >= 9.4
    caps = [r['capital_100m'] for r in gainers if r['capital_100m']]
    traits = {
        'n': len(gainers),
        'market': {m: sum(r['market'] == m for r in gainers) for m in ('上市', '上櫃')},
        'capital_100m': {'<10': sum(c < 10 for c in caps), '10-50': sum(10 <= c < 50 for c in caps),
                         '>=50': sum(c >= 50 for c in caps), 'median': r2(statistics.median(caps), 1) if caps else None},
        'price': {'<50': sum(r['close'] < 50 for r in gainers), '50-200': sum(50 <= r['close'] < 200 for r in gainers),
                  '>=200': sum(r['close'] >= 200 for r in gainers)},
        'turnover_100m_median': r2(statistics.median([r['value_100m'] for r in gainers]), 2) if gainers else None,
        'with_volume_spike_2x': sum((r['vol_ratio_5d'] or 0) >= 2 for r in gainers),
        'inst_net_buy': sum(1 for r in gainers if r['inst_lots'] and r['inst_lots']['all'] > 0),
        'disposal': sum(bool(r['disposal']) for r in gainers)}
    groups = {}
    for r in rows:
        if r['industry']:
            g = groups.setdefault(r['industry'], {'n': 0, 'sum': 0.0, 'up5': 0, 'up7': 0, 'value': 0.0})
            g['n'] += 1
            g['sum'] += r['chg_pct']
            g['up5'] += r['chg_pct'] >= 5
            g['up7'] += r['chg_pct'] >= 7
            g['value'] += r['value_100m']
    ind_stats = sorted(
        [{'industry': k, 'stocks': g['n'], 'avg_chg_pct': r2(g['sum'] / g['n']), 'n_up5': g['up5'], 'n_up7': g['up7'],
          'value_100m': r2(g['value'], 0)} for k, g in groups.items() if g['n'] >= 5],
        key=lambda x: (-x['n_up7'], -x['avg_chg_pct']))

    spikes = sorted([r for r in rows if r['vol_ratio_5d'] and r['vol_ratio_5d'] >= 2.5 and r['vol_lots'] >= 1000
                     and r['chg_pct'] >= 1.5 and r['value_100m'] >= 1],
                    key=lambda r: -r['value_100m'])

    def top(key, n=12):
        cand = [r for r in rows if r['inst_lots'] and r['inst_lots'][key] > 0]
        for r in cand:
            r[f'{key}_value_100m'] = r2(r['inst_lots'][key] * 1000 * r['close'] / 1e8, 2)
        return sorted(cand, key=lambda r: -r[f'{key}_value_100m'])[:n]

    inst_f, inst_t, inst_all = top('f'), top('t'), top('all')
    for r in inst_t:
        if r['capital_100m']:
            r['trust_pct_of_shares'] = r2(r['inst_lots']['t'] * 1000 / (r['capital_100m'] * 1e7) * 100, 3)
    both = sorted([r for r in rows if r['inst_lots'] and r['inst_lots']['f'] > 0 and r['inst_lots']['t'] > 0],
                  key=lambda r: -(r['inst_lots']['f'] + r['inst_lots']['t']) * r['close'])[:10]
    slim = lambda lst, n: [{k: v for k, v in r.items() if v not in (None, False, '')} for r in lst[:n]]
    return {
        'volume_days_used': days,
        'n_up7': len(gainers), 'n_limit_up': sum(r['limit_up'] for r in gainers),
        'gainer_traits': traits,
        'gainers': slim(gainers, 45),
        'industry_strength': ind_stats[:12],
        'volume_spikes': slim(spikes, 15),
        'inst_top_foreign': slim(inst_f, 10),
        'inst_top_trust': slim(inst_t, 10),
        'inst_top_total': slim(inst_all, 10),
        'foreign_and_trust_both_buy': slim(both, 8),
        'universe': len(by),
    }


# ---------------------------------------------------------------- 文字報告（給 Claude 讀）

def f0(x):
    return '—' if x is None else f'{x:,.0f}'


def sg(x, nd=0, unit=''):
    if x is None:
        return '—'
    return (f'{x:+,.{nd}f}' if nd else f'{x:+,.0f}') + unit


def price(x):
    if x is None:
        return '—'
    return f'{x:,.2f}'.rstrip('0').rstrip('.') if x < 1000 else f'{x:,.0f}'


def tech_lines(t):
    ma = t['ma']
    yield ('均線 ' + ' '.join(f'{k}:{price(v)}' for k, v in ma.items()) +
           f"｜KD {t['kd'][0]}/{t['kd'][1]}｜RSI {t['rsi14']}｜MACD DIF {t['macd']['dif']} DEA {t['macd']['dea']} 柱 {t['macd']['osc']}"
           f"｜布林 上{price(t['bb']['up'])} 下{price(t['bb']['low'])} %b {t['bb']['pct_b']} 寬{t['bb']['width_pct']}%"
           f"｜月線乖離 {sg(t['bias20_pct'], 1, '%')}")
    yield (f"報酬 5日 {sg(t['ret_pct']['5d'], 1, '%')} 20日 {sg(t['ret_pct']['20d'], 1, '%')} 60日 {sg(t['ret_pct']['60d'], 1, '%')}"
           f"｜20日高 {price(t['high20'])} 低 {price(t['low20'])}"
           f"｜52週高 {price(t['high52w'])}（距 {sg(t['from_52w_high_pct'], 1, '%')}） 低 {price(t['low52w'])}")
    yield '轉折點：' + ' → '.join(f'{d} {k}{price(v)}' for d, k, v in t['pivots'])


def render_holdings(out):
    m, L = out['meta'], []
    L.append('# 基本資訊')
    L.append(f"今天 {m['today']}（{m['weekday']}）｜台股交易日：{'是' if m['tw_trading_day'] else '否（休市）'}"
             f"｜今日法人資料：{'已公布' if m.get('tw_inst_ready') else '未取得'}")
    td = m.get('tdcc_dates') or {}
    L.append(f"集保週資料：{td.get('now', '—')}（比較 1週前 {td.get('1w', '—')}、4週前 {td.get('4w', '—')}）"
             f"｜主動ETF持股資料日：{m.get('active_etf_date', '—')}")
    L.append('抓取失敗：' + ('；'.join(out['errors']) if out['errors'] else '無'))
    mk = out.get('market')
    if mk and mk.get('taiex'):
        tx = mk['taiex']
        L += ['', '# 大盤',
              f"加權指數 {f0(tx['close'])}（{sg(tx['pct'], 2, '%')}，{'+' if (tx['pct'] or 0) >= 0 else '-'}{f0(tx['pts'])}點）"
              f"｜一般股票成交值 {f0(mk.get('value_100m'))} 億｜上漲 {mk.get('up', '—')} 下跌 {mk.get('down', '—')}（括號內為漲跌停）",
              '強勢類股：' + '、'.join(f"{x['name']} {sg(x['pct'], 2, '%')}" for x in mk.get('sectors_top', [])),
              '弱勢類股：' + '、'.join(f"{x['name']} {sg(x['pct'], 2, '%')}" for x in mk.get('sectors_bottom', []))]
    L += ['', '# 台股持股']
    for h in out['tw_holdings']:
        t, c = h.get('tech'), h.get('chips')
        L.append('')
        if not t:
            L.append(f"## {h['code']} {h.get('name', '')}：股價資料不足，無法計算")
            continue
        vol = (h.get('recent_bars') or [[0] * 6])[-1][5]
        L.append(f"## {h['code']} {h.get('name', '')}（{h.get('market', '')}）收 {price(t['close'])}（{sg(t['chg_pct'], 2, '%')}）"
                 f"量 {f0(vol)}張（5日量比 {t['vol_ratio_5d']}、20日量比 {t['vol_ratio_20d']}）資料日 {h.get('last_date')}"
                 + (f"｜⚠️處置中 {h['disposal']}（人工管制撮合）" if h.get('disposal') else ''))
        L.append(f"技術基礎分 {t['score']}（昨 {h.get('tech_score_yesterday', '—')}，原始 {t['raw']}/10）｜訊號：{'、'.join(t['signals']) or '無'}")
        L += list(tech_lines(t))
        L.append('近8日K（日期 開 高 低 收 量張）：' + '；'.join(
            ' '.join(price(x) if j in (1, 2, 3, 4) else str(x) for j, x in enumerate(b)) for b in h.get('recent_bars', [])))
        if c:
            L.append(f"籌碼基礎分 {c['score']}（昨 {h.get('chips_score_yesterday', '—')}，原始 {c['raw']}/{c['full']}）｜{'、'.join(c['notes']) or '—'}")
        ins = h.get('inst')
        if ins:
            tl, s5, s20 = ins.get('today_lots'), ins['sum5_lots'], ins['sum20_lots']
            today_txt = (f"今日 外資 {sg(tl['f'])}／投信 {sg(tl['t'])}／自營 {sg(tl['d'])}"
                         f"（外資+投信佔成交量 {sg(ins.get('today_ft_pct_of_volume'), 1, '%')}）"
                         if tl else f"今日資料未取得（最新 {ins['date']}）")
            L.append(f"法人（張）{today_txt}｜5日 外資 {sg(s5['f'])}／投信 {sg(s5['t'])}｜20日 外資 {sg(s20['f'])}／投信 {sg(s20['t'])}")
            L.append('近6日 外資/投信：' + '；'.join(f'{d} {sg(a)}/{sg(b)}' for d, a, b in ins['daily_ft_lots']))
        tc = h.get('tdcc')
        if tc:
            L.append(f"集保（{tc['date']}）：大戶≥400張 {tc['big400']}%（週 {sg(tc.get('d_big400_1w'), 2)}、4週 {sg(tc.get('d_big400_4w'), 2)}）"
                     f"｜千張大戶 {tc['big1000']}%（週 {sg(tc.get('d_big1000_1w'), 2)}、4週 {sg(tc.get('d_big1000_4w'), 2)}）"
                     f"｜散戶≤10張 {tc['small10']}%（週 {sg(tc.get('d_small10_1w'), 2)}、4週 {sg(tc.get('d_small10_4w'), 2)}）"
                     f"｜股東 {f0(tc.get('holders'))} 人（週 {sg(tc.get('d_holders_1w'))}）")
        e = h.get('active_etf')
        if e is not None:
            L.append(f"主動ETF：{e['etf_count']} 檔持有（合計 {e.get('total_value_100m', 0)} 億）"
                     f"{'｜在多檔同步加碼名單' if e['consensus_buy'] else ''}｜近期異動：{'；'.join(e['events']) or '無'}")
        L.append('新聞：' + ('' if h.get('news') else '無'))
        for n in h.get('news', []):
            L.append(f"- [{n['time']} {n.get('source') or ''}] {n['title']}")
    if out['us_holdings']:
        L += ['', '# 美股持股（價格為最近一個已收盤的美股交易日）']
    for u in out['us_holdings']:
        t, c = u.get('tech'), u.get('chips')
        L.append('')
        if not t:
            L.append(f"## {u['ticker']}：股價資料不足，無法計算")
            continue
        vol = (u.get('recent_bars') or [[0] * 6])[-1][5]
        L.append(f"## {u['ticker']} 收 {price(t['close'])}（{sg(t['chg_pct'], 2, '%')}）量 {vol}M 股（5日量比 {t['vol_ratio_5d']}）資料日 {u.get('last_date')}")
        L.append(f"技術基礎分 {t['score']}（昨 {u.get('tech_score_yesterday', '—')}，原始 {t['raw']}/10）｜訊號：{'、'.join(t['signals']) or '無'}")
        L += list(tech_lines(t))
        L.append('近8日K（日期 開 高 低 收 量M）：' + '；'.join(' '.join(str(x) for x in b) for b in u.get('recent_bars', [])))
        if c:
            L.append(f"籌碼基礎分（美股版）{c['score']}（原始 {c['raw']}/{c['full']}）")
        if u.get('finviz'):
            L.append('finviz：' + '、'.join(f'{k} {v}' for k, v in u['finviz'].items()))
        L.append('新聞：' + ('' if u.get('news') else '無'))
        for n in u.get('news', []):
            L.append(f"- [{n['time']} {n.get('source') or ''}] {n['title']}")
    return '\n'.join(L) + '\n'


def inst_txt(il):
    return f"外{sg(il['f'])} 投{sg(il['t'])} 自{sg(il['d'])}" if il else '法人—'


def flags(r):
    return (f"｜處置中 {r['disposal']}" if r.get('disposal') else '') + ('｜★我的持股' if r.get('mine') else '')


def render_hot(out):
    h = out.get('hot')
    if not h:
        return '今天沒有熱門股資料（休市或抓取失敗）。\n'
    tr = h['gainer_traits']
    cap, pr = tr['capital_100m'], tr['price']
    L = [f"# 今日熱門（{out['meta']['today']}）",
         f"全市場普通股 {h['universe']} 檔｜漲幅≥7%：{h['n_up7']} 檔（其中漲停約 {h['n_limit_up']} 檔）"
         f"｜5日均量基準日：{'、'.join(d[5:] for d in h['volume_days_used'])}",
         f"漲幅≥7% 的共同特徵：上市 {tr['market']['上市']}／上櫃 {tr['market']['上櫃']}"
         f"｜股本 <10億 {cap['<10']}、10–50億 {cap['10-50']}、≥50億 {cap['>=50']}（中位數 {cap['median']} 億）"
         f"｜股價 <50元 {pr['<50']}、50–200元 {pr['50-200']}、≥200元 {pr['>=200']}｜成交值中位數 {tr['turnover_100m_median']} 億"
         f"｜量比≥2 有 {tr['with_volume_spike_2x']} 檔｜法人合計買超 {tr['inst_net_buy']} 檔｜處置股 {tr['disposal']} 檔",
         '', '## 產業強度（證交所/櫃買官方分類，家數≥5；依漲≥7%家數排序）']
    for g in h['industry_strength']:
        L.append(f"{g['industry']}：{g['stocks']} 檔，平均 {sg(g['avg_chg_pct'], 2, '%')}，漲≥5% {g['n_up5']} 檔、"
                 f"≥7% {g['n_up7']} 檔，成交值 {f0(g['value_100m'])} 億")
    L += ['', '## 漲幅≥7% 個股（依成交值排序；股本單位億元）']
    for r in h['gainers']:
        L.append(f"{r['code']} {r['name']}｜{r['market']}｜{r.get('industry', '—')}｜{sg(r['chg_pct'], 2, '%')}{' 漲停' if r.get('limit_up') else ''}"
                 f"｜{price(r['close'])}元｜成交值 {r['value_100m']}億｜量比 {r.get('vol_ratio_5d', '—')}｜股本 {r.get('capital_100m', '—')}"
                 f"｜{inst_txt(r.get('inst_lots'))}{flags(r)}")
    L += ['', '## 爆量（量≥5日均量2.5倍、量≥1000張、漲≥1.5%、成交值≥1億；依成交值排序）']
    for r in h['volume_spikes']:
        L.append(f"{r['code']} {r['name']}｜{r['market']}｜{r.get('industry', '—')}｜{sg(r['chg_pct'], 2, '%')}｜量比 {r['vol_ratio_5d']}"
                 f"｜成交值 {r['value_100m']}億｜股本 {r.get('capital_100m', '—')}｜{inst_txt(r.get('inst_lots'))}{flags(r)}")
    for key, title, vk in (('inst_top_foreign', '外資買超前10（依金額）', 'f_value_100m'),
                           ('inst_top_trust', '投信買超前10（依金額）', 't_value_100m'),
                           ('inst_top_total', '三大法人合計買超前10（依金額）', 'all_value_100m')):
        L += ['', f'## {title}']
        for r in h[key]:
            extra = f"｜佔股本 {r['trust_pct_of_shares']}%" if key == 'inst_top_trust' and r.get('trust_pct_of_shares') else ''
            L.append(f"{r['code']} {r['name']}｜{r.get('industry', '—')}｜{sg(r['chg_pct'], 2, '%')}｜{r.get(vk)}億"
                     f"｜{inst_txt(r['inst_lots'])}{extra}")
    L += ['', '## 外資＋投信同步買超（依合計金額）']
    for r in h['foreign_and_trust_both_buy']:
        L.append(f"{r['code']} {r['name']}｜{r.get('industry', '—')}｜{sg(r['chg_pct'], 2, '%')}｜{inst_txt(r['inst_lots'])}")
    return '\n'.join(L) + '\n'


# ---------------------------------------------------------------- 推播

def split_body(body, limit=3800):
    if len(body.encode('utf-8')) <= limit:
        return [body]
    parts, cur = [], ''
    for block in body.split('\n\n'):
        cand = (cur + '\n\n' + block) if cur else block
        if len(cand.encode('utf-8')) <= limit:
            cur = cand
            continue
        if cur:
            parts.append(cur)
        while len(block.encode('utf-8')) > limit:
            cut = block[:limit // 4]
            parts.append(cut)
            block = block[len(cut):]
        cur = block
    if cur:
        parts.append(cur)
    return parts


def push(path, topic):
    """messages 依「閱讀順序」排列；倒過來送，讓第一則最後送出、在手機上排最上面。"""
    msgs = json.load(open(path, encoding='utf-8'))
    queue = []
    for m in msgs:
        parts = split_body(m['message'].strip())
        for i, part in enumerate(parts):
            queue.append((m, part, m['title'] + (f'（{i + 1}/{len(parts)}）' if len(parts) > 1 else '')))
    ok = True
    for m, part, title in reversed(queue):
        payload = {'topic': topic, 'title': title, 'message': part, 'priority': m.get('priority', 3)}
        for k in ('tags', 'click'):
            if m.get(k):
                payload[k] = m[k]
        data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        for attempt in range(2):
            try:
                req = urllib.request.Request('https://ntfy.sh/', data=data, headers={'Content-Type': 'application/json'})
                print(f'{urllib.request.urlopen(req, timeout=30).status} {title}（{len(part.encode())} bytes）')
                break
            except Exception as e:  # noqa: BLE001
                print(f'FAIL {title}: {e}')
                if attempt:
                    ok = False
                time.sleep(3)
        time.sleep(1.5)
    return ok


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--tw', default='')
    ap.add_argument('--us', default='')
    ap.add_argument('--date', help='模擬日期 YYYY-MM-DD（測試用）')
    ap.add_argument('--out', default='/tmp/stock', help='輸出資料夾')
    ap.add_argument('--skip-hot', action='store_true')
    ap.add_argument('--push', help='推播訊息 JSON 檔：[{title, message, priority, tags, click}]')
    ap.add_argument('--topic')
    a = ap.parse_args()
    if a.push:
        sys.exit(0 if push(a.push, a.topic) else 1)
    os.makedirs(a.out, exist_ok=True)
    t0 = time.time()
    result = run(a)
    result['meta']['seconds'] = round(time.time() - t0)
    with open(f'{a.out}/data.json', 'w', encoding='utf-8') as fh:
        json.dump(result, fh, ensure_ascii=False)
    for name, fn in (('holdings.txt', render_holdings), ('hot.txt', render_hot)):
        try:
            text = fn(result)
        except Exception as e:  # noqa: BLE001
            text = f'報告產生失敗：{e}\n'
            fail(name, e)
        with open(f'{a.out}/{name}', 'w', encoding='utf-8') as fh:
            fh.write(text)
    m = result['meta']
    print(f"完成，用時 {m['seconds']} 秒｜台股交易日：{m['tw_trading_day']}｜今日法人資料：{m.get('tw_inst_ready')}"
          f"｜台股持股 {len(result['tw_holdings'])} 檔、美股 {len(result['us_holdings'])} 檔｜熱門：{'有' if result.get('hot') else '無'}")
    print(f'報告：{a.out}/holdings.txt、{a.out}/hot.txt（原始資料 {a.out}/data.json）')
    print('抓取失敗：' + ('；'.join(ERRORS) if ERRORS else '無'))
    if BLOCKED:
        print('被雲端網路設定擋下的網域（要到環境設定的 Allowed domains 加入）：' + '、'.join(sorted(BLOCKED)))
