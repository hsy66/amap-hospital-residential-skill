#!/usr/bin/env python3
"""查询医院直线 5km 内的真实住宅小区（高德 Web 服务 API，仅标准库）。
用法: AMAP_KEY=xxx python3 amap_nearby_residential.py "武汉协和医院" "湖北省人民医院" --city 武汉 -n 10
"""
import argparse, csv, json, math, os, re, sys, time, urllib.parse, urllib.request

API = "https://restapi.amap.com/v5/place/"

def load_env():
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            if "=" in line and not line.startswith("#"):
                k, v = line.strip().split("=", 1)
                os.environ.setdefault(k, v)

def get(path, **params):
    params["key"] = os.environ["AMAP_KEY"]
    url = (path if path.startswith("http") else API + path) + "?" + urllib.parse.urlencode(params)
    for i in range(3):
        d = json.load(urllib.request.urlopen(url, timeout=20))
        if d.get("status") == "1":
            return d
        if d.get("info") in ("CUQPS_HAS_EXCEEDED_THE_LIMIT", "ACCESS_TOO_FREQUENT"):
            time.sleep(1 + i); continue
        raise SystemExit(f"高德 API 错误: {d.get('info')} (infocode {d.get('infocode')})")
    raise SystemExit("高德 API 限流，重试失败")

def haversine(a, b):
    (x1, y1), (x2, y2) = a, b
    r = math.radians
    h = math.sin(r(y2 - y1) / 2) ** 2 + math.cos(r(y1)) * math.cos(r(y2)) * math.sin(r(x2 - x1) / 2) ** 2
    return 2 * 6371008.8 * math.asin(math.sqrt(h))

def loc(s):
    x, y = s.split(","); return float(x), float(y)

def resolve(q, city):
    p = dict(keywords=q, types="090100|090101|090102|0901", page_size=5)
    if city: p.update(region=city, city_limit="true")
    pois = get("text", **p).get("pois") or []
    if pois:
        h = pois[0]
        return dict(name=h["name"], address=h.get("address", ""), location=loc(h["location"]))
    g = get("https://restapi.amap.com/v3/geocode/geo", address=q, **({"city": city} if city else {}))
    if g.get("geocodes"):
        h = g["geocodes"][0]
        return dict(name=q, address=h.get("formatted_address", ""), location=loc(h["location"]))
    raise SystemExit(f"无法定位医院: {q}")

CN = "零〇一二三四五六七八九十百两"
BAD = re.compile(r"(停车场|出入口|入口|出口|公司|写字楼|大厦|商铺|商业|酒店|宾馆|学校|幼儿园|小学|中学|医院|诊所|门诊|办公|厂|仓库|物业|社区$|居委会|活动室|服务站|服务中心|党群|村委会|还建楼$|售楼|营销中心|示范区|地块|工地|在建|筹建)")
def base_name(n):
    s = re.sub(r"[（(\[【].*?[)）\]】]", "", n).strip()
    s = re.sub(r"[-—·\s]+", "", s)
    pat = re.compile(rf"(([{CN}0-9]+|[A-Za-z]+|[东西南北中上下内外新老][东西南北]?)(期|区|苑|座|组团|片区|号院|院|栋|号楼|幢|门|街坊)|[{CN}0-9A-Za-z]+(栋|号|号楼|单元)?|[东西南北]门|(东|西|南|北|中)(区|苑|院))$")
    prev = None
    while prev != s and len(s) > 2:
        prev = s
        s2 = pat.sub("", s)
        if len(s2) >= 2: s = s2
    s = re.sub(r"(小区|社区|家属院|家属区|宿舍|公寓|花园|花苑|新村|新城|住宅区)$", "", s) or s
    return s

args_max_pages = 20

def similar(b, used):
    """前缀包含视为同一小区系列，例如 湖北邮电 / 湖北邮电新华街…"""
    return any(b == u or (min(len(b), len(u)) >= 3 and (b.startswith(u) or u.startswith(b))) for u in used)

def nearby(center, radius):
    out, page = {}, 1
    while True:
        d = get("around", location=f"{center[0]:.6f},{center[1]:.6f}", radius=radius,
                types="120300|120302", page_size=25, page_num=page, sortrule="distance")
        pois = d.get("pois") or []
        for p in pois:
            if not str(p.get("typecode", "")).startswith("1203"): continue
            if BAD.search(re.sub(r"[（(].*?[)）]", "", p["name"])): continue
            dist = haversine(center, loc(p["location"]))
            if dist <= radius: out[p["id"]] = dict(p, dist=round(dist))
        if len(pois) < 25 or page >= args_max_pages: break
        page += 1
    return sorted(out.values(), key=lambda p: p["dist"])

def main():
    load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("hospitals", nargs="+")
    ap.add_argument("--city", default="")
    ap.add_argument("-n", "--per-hospital", type=int, default=10)
    ap.add_argument("-r", "--radius", type=int, default=5000)
    ap.add_argument("--format", choices=["md", "json", "csv"], default="md")
    ap.add_argument("--max-pages", type=int, default=20, help="每家医院周边搜索最多翻页数(每页25条)")
    a = ap.parse_args()
    global args_max_pages; args_max_pages = a.max_pages
    if not os.environ.get("AMAP_KEY"): raise SystemExit("请设置环境变量 AMAP_KEY（高德 Web服务 key）")
    used, cache, rows = set(), {}, []
    for q in a.hospitals:
        if q not in cache:
            h = resolve(q, a.city); cache[q] = (h, nearby(h["location"], a.radius))
        h, cands = cache[q]; got = 0
        for p in cands:
            b = base_name(p["name"])
            if similar(b, used): continue
            used.add(b); got += 1
            x, y = loc(p["location"])
            rows.append(dict(hospital_query=q, hospital=h["name"], hospital_address=h["address"],
                compound=p["name"], address=p.get("address", ""), district=p.get("adname", ""),
                distance_m=p["dist"], lng=x, lat=y))
            if got >= a.per_hospital: break
        if got < a.per_hospital: print(f"提示: {q} 5km 内仅找到 {got} 个不重复小区", file=sys.stderr)
    if a.format == "json": print(json.dumps(rows, ensure_ascii=False, indent=2))
    elif a.format == "csv":
        w = csv.DictWriter(sys.stdout, fieldnames=list(rows[0]) if rows else ["hospital"]); w.writeheader(); w.writerows(rows)
    else:
        print("| 查询医院 | 定位医院 | 小区 | 地址 | 区 | 直线距离(m) | 经纬度 |\n|---|---|---|---|---|---|---|")
        for r in rows:
            print(f"| {r['hospital_query']} | {r['hospital']} | {r['compound']} | {r['address']} | {r['district']} | {r['distance_m']} | {r['lng']},{r['lat']} |")

if __name__ == "__main__":
    main()
