#!/usr/bin/env python3
"""医院直线 5km 内真实住宅小区（高德 Web 服务 API，仅标准库，快速版）。
python3 amap_nearby_residential.py 武汉协和医院 武汉协和医院 湖北省人民医院 --city 武汉 -n 10 [--format md|json|csv] [--seed 1]
"""
import argparse, csv, json, math, os, random, re, sys, time, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor

RADIUS, MIN_D, SUB_R, TIMEOUT = 5000, 500, 700, 8
# 采样点 (距离m, 方位°)：1.5/3/4.5km 三个环 × 4 个方向交错，共 12 个，外加医院本身
POINTS = [(1500, b) for b in (45, 135, 225, 315)] + [(3000, b) for b in (0, 90, 180, 270)] + \
         [(4300, b) for b in (45, 135, 225, 315)]
SECTORS = ["北", "东北", "东", "东南", "南", "西南", "西", "西北"]
CALLS = 0
BAD = re.compile(r"(停车场|出入口|入口|出口|公司|写字楼|大厦|大夏|大楼|座$|商铺|商业|酒店|宾馆|学校|幼儿园|小学|中学|"
                 r"医院|诊所|办公|厂|仓库|物业|售楼|营销中心|地块|工地|社区$|居委会|活动室|服务站|服务中心|党群|还建楼$|"
                 r"大道$|路$|街$|[路街道]-|建设中|在建|待售|拆迁)")

def load_env():
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            if "=" in line and not line.startswith("#"):
                k, v = line.strip().split("=", 1); os.environ.setdefault(k, v)

def get(url, **params):
    global CALLS
    params["key"] = os.environ["AMAP_KEY"]
    url = url + "?" + urllib.parse.urlencode(params)
    for i in range(4):
        CALLS += 1
        try:
            d = json.load(urllib.request.urlopen(url, timeout=TIMEOUT))
        except Exception as e:
            if i == 3: raise SystemExit(f"网络错误: {e}")
            continue
        if d.get("status") == "1": return d
        if "LIMIT" in d.get("info", "") or "FREQUENT" in d.get("info", ""):
            time.sleep(0.4 + random.random() * 0.4); continue
        raise SystemExit(f"高德 API 错误: {d.get('info')} (infocode {d.get('infocode')})")
    return {}

def loc(s):
    x, y = s.split(","); return float(x), float(y)

def haversine(a, b):
    r = math.radians
    h = math.sin(r(b[1] - a[1]) / 2) ** 2 + math.cos(r(a[1])) * math.cos(r(b[1])) * math.sin(r(b[0] - a[0]) / 2) ** 2
    return 2 * 6371008.8 * math.asin(math.sqrt(h))

def bearing(a, b):
    dx = (b[0] - a[0]) * math.cos(math.radians(a[1])); dy = b[1] - a[1]
    return (math.degrees(math.atan2(dx, dy)) + 360) % 360

def offset(c, d, brg):
    return (c[0] + d * math.sin(math.radians(brg)) / (111320 * math.cos(math.radians(c[1]))),
            c[1] + d * math.cos(math.radians(brg)) / 110540)

def resolve(q, city):
    p = dict(keywords=q, types="090100|090101|090102|0901", page_size=1)
    if city: p.update(region=city, city_limit="true")
    pois = get("https://restapi.amap.com/v5/place/text", **p).get("pois") or []
    if pois:
        return dict(name=pois[0]["name"], address=pois[0].get("address", ""), location=loc(pois[0]["location"]))
    g = get("https://restapi.amap.com/v3/geocode/geo", address=q, **({"city": city} if city else {})).get("geocodes") or []
    if not g: raise SystemExit(f"无法定位医院: {q}")
    return dict(name=q, address=g[0].get("formatted_address", ""), location=loc(g[0]["location"]))

CN = "零〇一二三四五六七八九十百两"
SUFFIX = re.compile(rf"(([{CN}0-9]+|[A-Za-z]+|[东西南北中上下内外新老][东西南北]?)(期|区|苑|座|组团|片区|号院|院|栋|号楼|幢|门|街坊)"
                    rf"|[{CN}0-9A-Za-z]+(栋|号|号楼|单元)?|[东西南北]门)$")
def base_name(n):
    s = re.sub(r"[（(\[【].*?[)）\]】]", "", n)
    s = re.split(r"[-—]", s)[0]                      # 去掉 "-xxx" 子名
    s = re.sub(r"[·\s]+", "", s)
    prev = None
    while prev != s and len(s) > 2:
        prev = s; t = SUFFIX.sub("", s); s = t if len(t) >= 2 else s
    return re.sub(r"(小区|社区|家属院|家属区|宿舍|公寓|花园|花苑|新村|新城|住宅区)$", "", s) or s

def similar(b, used):
    return any(b == u or (min(len(b), len(u)) >= 3 and (b.startswith(u) or u.startswith(b))) for u in used)

def fetch_pool(center, page):
    """并发对 13 个采样点各取 1 页（每页 25 条）。"""
    pts = [center] + [offset(center, d, b) for d, b in POINTS]
    def one(pt):
        return get("https://restapi.amap.com/v5/place/around", location=f"{pt[0]:.6f},{pt[1]:.6f}",
                   radius=SUB_R if pt is not center else 1200, types="120302|120300",
                   page_size=25, page_num=page).get("pois") or []
    pool = {}
    with ThreadPoolExecutor(6) as ex:  # 超 QPS 自动退避重试
        for lst in ex.map(one, pts):
            for p in lst:
                if not str(p.get("typecode", "")).startswith("1203"): continue
                if BAD.search(re.sub(r"[（(].*?[)）]", "", p["name"])): continue
                pl = loc(p["location"]); d = haversine(center, pl)
                if MIN_D <= d <= RADIUS:
                    pool[p["id"]] = dict(p, dist=round(d), sector=SECTORS[int(((bearing(center, pl) + 22.5) % 360) // 45)])
    return pool

def pick(pool, n, used, used_ids, rng):
    """按 1km 距离环 × 8 方位分桶，打乱后轮询，每桶随机取。"""
    buckets = {}
    for p in pool.values():
        if p["id"] not in used_ids: buckets.setdefault((p["dist"] // 1000, p["sector"]), []).append(p)
    for b in buckets.values(): rng.shuffle(b)
    keys = list(buckets); rng.shuffle(keys); out = []
    while len(out) < n and any(buckets[k] for k in keys):
        for k in keys:
            while buckets[k]:
                p = buckets[k].pop(); b = base_name(p["name"])
                if similar(b, used): continue
                used.add(b); used_ids.add(p["id"]); out.append(p); break
            if len(out) >= n: break
    return out

def main():
    load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("hospitals", nargs="+"); ap.add_argument("--city", default="")
    ap.add_argument("-n", type=int, default=10); ap.add_argument("--format", choices=["md", "json", "csv"], default="md")
    ap.add_argument("--seed", type=int)
    a = ap.parse_args()
    if not os.environ.get("AMAP_KEY"): raise SystemExit("请设置 AMAP_KEY（高德 Web服务 key）")
    rng, used, used_ids, memo, rows, t0 = random.Random(a.seed), set(), set(), {}, [], time.time()
    for q in a.hospitals:
        if q not in memo:
            h = resolve(q, a.city); memo[q] = [h, fetch_pool(h["location"], 1), 1]
        h, pool, pg = memo[q]
        got = pick(pool, a.n, used, used_ids, rng)
        if len(got) < a.n and pg == 1:              # 不够就每个采样点再取第 2 页，仅一次
            pool.update(fetch_pool(h["location"], 2)); memo[q][2] = 2
            got += pick(pool, a.n - len(got), used, used_ids, rng)
        if len(got) < a.n: print(f"提示: {q} 只找到 {len(got)}/{a.n} 个不重复小区", file=sys.stderr)
        for p in sorted(got, key=lambda p: p["dist"]):
            x, y = loc(p["location"])
            rows.append(dict(hospital_query=q, hospital=h["name"], hospital_address=h["address"], compound=p["name"],
                             address=p.get("address", ""), district=p.get("adname", ""), direction=p["sector"],
                             distance_m=p["dist"], lng=x, lat=y))
    print(f"API 调用 {CALLS} 次，用时 {time.time() - t0:.1f}s", file=sys.stderr)
    if a.format == "json": print(json.dumps(rows, ensure_ascii=False, indent=2))
    elif a.format == "csv":
        w = csv.DictWriter(sys.stdout, fieldnames=list(rows[0]) if rows else ["hospital"]); w.writeheader(); w.writerows(rows)
    else:
        print("| 查询医院 | 定位医院 | 小区 | 地址 | 区 | 方位 | 直线距离(m) | 经纬度 |\n|---|---|---|---|---|---|---|---|")
        for r in rows:
            print(f"| {r['hospital_query']} | {r['hospital']} | {r['compound']} | {r['address']} | {r['district']} | {r['direction']} | {r['distance_m']} | {r['lng']},{r['lat']} |")

if __name__ == "__main__":
    main()
