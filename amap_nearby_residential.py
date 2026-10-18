#!/usr/bin/env python3
"""医院直线 5km 内住宅小区（高德 Web 服务，仅标准库）。

每要一个小区才发一次周边搜索，凑够就停。医院定位走地理编码，不占搜索配额。
python3 amap_nearby_residential.py 武汉协和医院 武汉协和医院 --city 武汉 -n 1
"""
import argparse, csv, json, math, os, random, re, sys, time, urllib.parse, urllib.request

RADIUS, INNER, SEARCH_R, TIMEOUT = 5000, 800, 800, 8
GEOCODE = "https://restapi.amap.com/v3/geocode/geo"
AROUND = "https://restapi.amap.com/v5/place/around"
# 8 个方向；两圈按面积切开（外圈更大），不要按 1 公里切
BEARINGS = (0, 45, 90, 135, 180, 225, 270, 315)
RING_EDGE = math.sqrt((INNER * INNER + RADIUS * RADIUS) / 2)  # 约 3580m
RINGS = ((INNER, RING_EDGE), (RING_EDGE, RADIUS))
CALLS = {"geocode": 0, "search": 0}

BAD = re.compile(
    r"(停车场|车库|出入口|公司|写字楼|大厦|大楼|商铺|底商|商业|酒店|宾馆|学校|幼儿园|小学|中学|"
    r"医院|诊所|办公|厂房|仓库|物业|售楼|营销中心|地块|工地|居委会|活动室|服务站|服务中心|"
    r"党群|宿舍|公寓|还建|大道|路$|街$|建设中|在建|待售|拆迁)"
)
CN = "零〇一二三四五六七八九十百两"
# 只剥「2期 / 东区 / A座 / 1号楼 / 北门」这种分期尾巴。
# 不剥光秃秃的数字，否则阳光100 会被收成阳光，再和阳光城撞在一起。
SUFFIX = re.compile(
    rf"(([{CN}0-9]+|[A-Za-z]+|[东西南北中])(期|区|苑|座|组团|片区|号院|院|栋|号楼|幢|街坊)"
    rf"|[{CN}0-9]+(号楼|栋|幢|单元|号院|号)"
    rf"|[A-Za-z](座|栋|区|幢)"
    rf"|[东西南北]门)$"
)


def load_env():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def get(url, kind, **params):
    params["key"] = os.environ["AMAP_KEY"]
    full = url + "?" + urllib.parse.urlencode(params)
    for attempt in range(3):
        CALLS[kind] += 1
        try:
            data = json.load(urllib.request.urlopen(full, timeout=TIMEOUT))
        except Exception as exc:
            if attempt == 2:
                raise SystemExit(f"网络错误: {exc}")
            time.sleep(0.6)
            continue
        if str(data.get("status")) == "1":
            return data
        info = str(data.get("info", ""))
        # 只有访问过于频繁才退避重试。配额用尽（10003/10044 等）重试也没用
        if info in ("ACCESS_TOO_FREQUENT", "CQPS_HAS_EXCEEDED_THE_LIMIT",
                    "CKQPS_HAS_EXCEEDED_THE_LIMIT", "CUQPS_HAS_EXCEEDED_THE_LIMIT"):
            time.sleep(0.8 + random.random())
            continue
        raise SystemExit(f"高德 API 错误: {info} (infocode {data.get('infocode')})")
    return {}


def loc(text):
    lng, lat = text.split(",")
    return float(lng), float(lat)


def haversine(a, b):
    rad = math.radians
    h = math.sin(rad(b[1] - a[1]) / 2) ** 2 + math.cos(rad(a[1])) * math.cos(rad(b[1])) * math.sin(
        rad(b[0] - a[0]) / 2
    ) ** 2
    return 2 * 6371008.8 * math.asin(math.sqrt(h))


def offset(center, dist, bearing):
    lng = center[0] + dist * math.sin(math.radians(bearing)) / (111320 * math.cos(math.radians(center[1])))
    lat = center[1] + dist * math.cos(math.radians(bearing)) / 110540
    return lng, lat


def base_name(name):
    text = re.sub(r"[（(\[【].*?[)）\]】]", "", name)
    text = re.split(r"[-—]", text)[0]
    text = re.sub(r"[·\s]+", "", text)
    prev = None
    while prev != text and len(text) > 2:
        prev = text
        stripped = SUFFIX.sub("", text)
        text = stripped if len(stripped) >= 2 else text
    text = re.sub(r"(小区|社区|家属院|家属区|花园|花苑|新村|住宅区)$", "", text)
    return text or name


def same_series(a, b):
    if a == b:
        return True
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    # 只把「名字 + 很短的尾巴」当成同一处，避免阳光100 和 阳光城 被误伤
    return len(short) >= 3 and long.startswith(short) and len(long) - len(short) <= 2


def resolve(query, city):
    params = {"address": query}
    if city:
        params["city"] = city
    rows = get(GEOCODE, "geocode", **params).get("geocodes") or []
    if not rows or not rows[0].get("location"):
        raise SystemExit(f"无法定位医院: {query}")
    row = rows[0]
    return dict(name=query, address=row.get("formatted_address", ""), location=loc(row["location"]))


def slots(rng):
    """外圈、内圈轮流，方向打乱。每格可抽两次，第二次换一个点。"""
    order = []
    bearings = list(BEARINGS)
    rng.shuffle(bearings)
    for bearing in bearings:
        order.append((1, bearing))
        order.append((0, bearing))
    order *= 2
    return order


def sample_center(hospital, ring, bearing, rng):
    near, far = RINGS[ring]
    # 按面积均匀：外圈不会被抽空，也不会老抽到这一圈的内侧
    dist = math.sqrt(rng.random() * (far * far - near * near) + near * near)
    jitter = rng.uniform(-18, 18)
    return offset(hospital, dist, (bearing + jitter) % 360)


def acceptable(poi, hospital):
    if not str(poi.get("typecode", "")).startswith(("120302", "120301")):
        return None
    name = re.sub(r"[（(].*?[)）]", "", poi.get("name", ""))
    if not name or BAD.search(name):
        return None
    if not poi.get("location"):
        return None
    point = loc(poi["location"])
    dist = haversine(hospital, point)
    if not INNER <= dist <= RADIUS:
        return None
    return point, round(dist)


def search_one(center):
    data = get(
        AROUND,
        "search",
        location=f"{center[0]:.6f},{center[1]:.6f}",
        radius=SEARCH_R,
        types="120302|120301",
        sortrule="distance",
        page_size=25,
        page_num=1,
    )
    return data.get("pois") or []


def take_one(pois, hospital, used_names, used_ids, used_parents):
    ranked = []
    for poi in pois:
        judged = acceptable(poi, hospital)
        if not judged:
            continue
        try:
            ranked.append((float(poi.get("distance") or 10**9), poi, judged))
        except ValueError:
            ranked.append((10**9, poi, judged))
    for _, poi, (point, dist) in sorted(ranked, key=lambda item: item[0]):
        if poi.get("id") in used_ids:
            continue
        parent = poi.get("parent") or ""
        if parent and parent in used_parents:
            continue
        series = base_name(poi["name"])
        if any(same_series(series, old) for old in used_names):
            continue
        used_ids.add(poi.get("id"))
        if parent:
            used_parents.add(parent)
        used_names.add(series)
        return poi, point, dist, series
    return None


def collect(hospital, need, state, rng):
    got = []
    attempts = 0
    limit = max(need * 2, need)
    while len(got) < need and attempts < limit:
        ring, bearing = state["slots"][state["cursor"] % len(state["slots"])]
        state["cursor"] += 1
        attempts += 1
        pois = search_one(sample_center(hospital, ring, bearing, rng))
        picked = take_one(pois, hospital, state["names"], state["ids"], state["parents"])
        if picked:
            poi, point, dist, _series = picked
            got.append((poi, point, dist))
    return got


def main():
    load_env()
    parser = argparse.ArgumentParser(description="医院周边 5km 住宅小区，按需搜索")
    parser.add_argument("hospitals", nargs="+", help="医院名称或地址；重复一次表示再要一批不同的小区")
    parser.add_argument("--city", default="", help="城市名，避免同名医院定位错")
    parser.add_argument("-n", type=int, default=1, help="每一行要几个小区，默认 1")
    parser.add_argument("--format", choices=["md", "json", "csv"], default="md")
    parser.add_argument("--seed", type=int)
    args = parser.parse_args()
    if args.n < 1:
        raise SystemExit("-n 至少为 1")
    if not os.environ.get("AMAP_KEY"):
        raise SystemExit("请设置 AMAP_KEY（高德 Web 服务 key），或在脚本同目录放 .env")

    rng = random.Random(args.seed)
    memo, rows = {}, []
    started = time.time()
    for query in args.hospitals:
        if query not in memo:
            memo[query] = {
                "hospital": resolve(query, args.city),
                "slots": slots(rng),
                "cursor": 0,
                "names": set(),
                "ids": set(),
                "parents": set(),
            }
        state = memo[query]
        hospital = state["hospital"]
        got = collect(hospital["location"], args.n, state, rng)
        if len(got) < args.n:
            print(f"提示: {query} 只找到 {len(got)}/{args.n} 个不重复小区", file=sys.stderr)
        for poi, point, dist in sorted(got, key=lambda item: item[2]):
            bearing = (math.degrees(math.atan2(
                (point[0] - hospital["location"][0]) * math.cos(math.radians(hospital["location"][1])),
                point[1] - hospital["location"][1],
            )) + 360) % 360
            direction = ["北", "东北", "东", "东南", "南", "西南", "西", "西北"][int(((bearing + 22.5) % 360) // 45)]
            rows.append(dict(
                hospital_query=query,
                hospital=hospital["name"],
                hospital_address=hospital["address"],
                compound=poi["name"],
                address=poi.get("address", ""),
                district=poi.get("adname", ""),
                direction=direction,
                distance_m=dist,
                lng=point[0],
                lat=point[1],
            ))

    print(
        f"地理编码 {CALLS['geocode']} 次（不占搜索配额），周边搜索 {CALLS['search']} 次，用时 {time.time() - started:.1f}s",
        file=sys.stderr,
    )
    if args.format == "json":
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    elif args.format == "csv":
        writer = csv.DictWriter(sys.stdout, fieldnames=list(rows[0]) if rows else ["hospital_query"])
        writer.writeheader()
        writer.writerows(rows)
    else:
        print("| 查询医院 | 定位医院 | 小区 | 地址 | 区 | 方位 | 直线距离(m) | 经纬度 |")
        print("|---|---|---|---|---|---|---|---|")
        for row in rows:
            print(
                f"| {row['hospital_query']} | {row['hospital']} | {row['compound']} | {row['address']} | "
                f"{row['district']} | {row['direction']} | {row['distance_m']} | {row['lng']:.6f},{row['lat']:.6f} |"
            )


if __name__ == "__main__":
    main()
