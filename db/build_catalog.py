# -*- coding: utf-8 -*-
"""
build_catalog.py — 元数据底座登记（里程碑 M2）
对 demo_retail / chinook 做 introspection，写入 datamind_meta:
  data_sources / schema_snapshots / schema_tables / schema_columns /
  schema_relations / business_metrics / entity_aliases

用法: py -3.13 db/build_catalog.py
"""
import hashlib
import json
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras

PG = dict(host="localhost", port=5432, user="postgres", password="datamind_dev")
SNAPSHOT_TIME = datetime.now(timezone.utc)

# ============================================================
# 业务词表（business_name / aliases / 语义角色）
# 首版人工维护；后续可由元数据服务增量采集
# ============================================================
RETAIL_TABLES = {
    "regions":      {"cn": "销售大区", "aliases": ["大区", "区域", "地区"]},
    "products":     {"cn": "产品表", "aliases": ["商品", "货品", "产品目录"]},
    "customers":    {"cn": "客户表", "aliases": ["会员", "顾客", "买家"]},
    "orders":       {"cn": "订单表", "aliases": ["订单", "购买记录", "销售订单"]},
    "order_items":  {"cn": "订单明细表", "aliases": ["明细", "订单行", "成交明细"]},
    "refunds":      {"cn": "退款表", "aliases": ["退货", "退款记录"]},
}

RETAIL_COLUMNS = {
    ("regions", "region_id"):      {"cn": "大区ID", "role": "id"},
    ("regions", "region_name"):    {"cn": "大区名称", "role": "dimension", "aliases": ["区域", "地区", "大区"]},
    ("regions", "capital"):        {"cn": "省会城市", "role": "dimension", "aliases": ["首府"]},
    ("products", "product_id"):    {"cn": "产品ID", "role": "id"},
    ("products", "product_name"):  {"cn": "产品名称", "role": "dimension", "aliases": ["商品名", "品名"]},
    ("products", "category"):      {"cn": "产品类别", "role": "dimension", "aliases": ["品类", "类目", "分类"],
                                    "enum": {"家电": "家电", "数码": "数码", "家居": "家居", "食品": "食品"}},
    ("products", "brand"):         {"cn": "品牌", "role": "dimension"},
    ("products", "list_price"):    {"cn": "标价", "role": "metric", "unit": "CNY", "aliases": ["原价", "定价", "零售价"]},
    ("products", "cost_price"):    {"cn": "成本价", "role": "metric", "unit": "CNY", "aliases": ["进价", "采购价"]},
    ("products", "unit"):          {"cn": "计量单位", "role": "dimension"},
    ("products", "launch_date"):   {"cn": "上市日期", "role": "time", "aliases": ["发布日期"]},
    ("products", "description"):   {"cn": "产品描述", "role": "text"},
    ("customers", "customer_id"):     {"cn": "客户ID", "role": "id"},
    ("customers", "customer_name"):   {"cn": "客户姓名", "role": "dimension", "aliases": ["顾客姓名", "会员名", "买家昵称"]},
    ("customers", "phone"):           {"cn": "手机号", "role": "text", "sens": "restricted", "aliases": ["电话", "联系方式"]},
    ("customers", "region_id"):       {"cn": "所属大区", "role": "dimension"},
    ("customers", "city"):            {"cn": "城市", "role": "dimension"},
    ("customers", "customer_level"):  {"cn": "客户等级", "role": "dimension", "aliases": ["会员等级", "客户级别"],
                                       "enum": {"普通": "普通", "银卡": "银卡", "金卡": "金卡", "钻石": "钻石"}},
    ("customers", "registered_at"):   {"cn": "注册日期", "role": "time", "aliases": ["注册时间"]},
    ("orders", "order_id"):        {"cn": "订单ID", "role": "id"},
    ("orders", "customer_id"):     {"cn": "下单客户", "role": "dimension"},
    ("orders", "order_status"):    {"cn": "订单状态", "role": "dimension", "aliases": ["状态"],
                                    "enum": {"pending": "待支付", "paid": "已支付", "shipped": "已发货",
                                             "completed": "已完成", "cancelled": "已取消", "refunded": "已退款"}},
    ("orders", "channel"):         {"cn": "销售渠道", "role": "dimension", "aliases": ["渠道", "下单渠道"],
                                    "enum": {"online": "线上商城", "app": "APP", "offline": "线下门店"}},
    ("orders", "created_at"):      {"cn": "下单时间", "role": "time", "aliases": ["订单日期", "下单日期", "购买时间"]},
    ("orders", "paid_at"):         {"cn": "支付时间", "role": "time", "aliases": ["付款时间"]},
    ("orders", "remark"):          {"cn": "订单备注", "role": "text"},
    ("order_items", "order_item_id"): {"cn": "明细ID", "role": "id"},
    ("order_items", "order_id"):      {"cn": "所属订单", "role": "dimension"},
    ("order_items", "product_id"):    {"cn": "产品", "role": "dimension"},
    ("order_items", "quantity"):      {"cn": "购买数量", "role": "metric", "unit": "件", "aliases": ["数量", "件数", "销量"]},
    ("order_items", "unit_price"):    {"cn": "成交单价", "role": "metric", "unit": "CNY", "aliases": ["单价", "实际单价"]},
    ("order_items", "discount"):      {"cn": "折扣率", "role": "dimension", "aliases": ["折扣", "优惠"]},
    ("refunds", "refund_id"):      {"cn": "退款ID", "role": "id"},
    ("refunds", "order_item_id"):  {"cn": "退款明细", "role": "dimension"},
    ("refunds", "refund_amount"):  {"cn": "退款金额", "role": "metric", "unit": "CNY", "aliases": ["退款额", "退货金额"]},
    ("refunds", "reason"):         {"cn": "退款原因", "role": "dimension"},
    ("refunds", "refunded_at"):    {"cn": "退款时间", "role": "time", "aliases": ["退货时间"]},
}

RETAIL_RELATIONS = [
    # (from_table, from_cols, to_table, to_cols, cardinality, kind, approved, description)
    ("orders", ["customer_id"], "customers", ["customer_id"], "n:1", "foreign_key", True, "订单归属客户"),
    ("customers", ["region_id"], "regions", ["region_id"], "n:1", "foreign_key", True, "客户归属大区（可空）"),
    ("order_items", ["order_id"], "orders", ["order_id"], "n:1", "foreign_key", True, "明细归属订单"),
    ("order_items", ["product_id"], "products", ["product_id"], "n:1", "foreign_key", True, "明细对应产品"),
    ("refunds", ["order_item_id"], "order_items", ["order_item_id"], "n:1", "foreign_key", True, "退款对应明细"),
    ("refunds", ["order_item_id"], "orders", ["order_id"], "n:1", "business", False,
     "禁止直连：退款→明细→订单须经过 order_items.order_id"),
]

RETAIL_METRICS = [
    # (metric_id, name, aliases, definition, expression, unit, grain)
    ("sales_amount", "销售额", ["销售金额", "营收", "GMV", "成交额", "业绩"],
     "有效订单（completed/shipped/refunded）下所有明细的 quantity × unit_price × (1 - discount) 之和",
     "sum(oi.quantity * oi.unit_price * (1 - oi.discount))", "CNY", "订单明细"),
    ("units_sold", "销量", ["销售数量", "售出件数", "卖出多少"],
     "有效订单下所有明细的 quantity 之和",
     "sum(oi.quantity)", "件", "订单明细"),
    ("refund_amount", "退款金额", ["退款额", "退货金额"],
     "refunds.refund_amount 直接求和（不区分订单状态）",
     "sum(r.refund_amount)", "CNY", "退款单"),
    ("order_count", "订单数", ["订单量", "单量"],
     "count(distinct order_id)，cancelled 订单是否计入需澄清（默认计入已创建订单）",
     "count(distinct o.order_id)", "单", "订单"),
    ("net_sales", "净销售额", ["净收入", "扣除退款后的销售额"],
     "销售额 - 退款金额（同一时间范围口径）",
     "sales_amount - refund_amount", "CNY", "订单明细"),
    ("customer_count", "客户数", ["客户总量", "会员数", "有多少客户"],
     "count(distinct customer_id)", "count(distinct customer_id)", "人", "客户"),
]

RETAIL_ALIASES = [
    # (canonical_id, canonical_name, entity_type, alias)
    ("region.east", "华东", "region", "华东地区"),
    ("region.east", "华东", "region", "东部"),
    ("region.north", "华北", "region", "华北地区"),
    ("region.south", "华南", "region", "华南地区"),
    ("region.central", "华中", "region", "华中地区"),
    ("region.southwest", "西南", "region", "西南地区"),
    ("region.northeast", "东北", "region", "东北地区"),
    ("level.diamond", "钻石", "customer_level", "VIP"),
    ("level.diamond", "钻石", "customer_level", "黑卡"),
    ("level.gold", "金卡", "customer_level", "高级会员"),
    ("channel.offline", "offline", "channel", "线下"),
    ("channel.offline", "offline", "channel", "门店"),
    ("channel.online", "online", "channel", "线上"),
    ("channel.app", "app", "channel", "手机APP"),
]

# Chinook 中文业务词表（公开示例库，用于中文问数）
CHINOOK_TABLES = {
    "artist":        {"cn": "艺术家", "aliases": ["歌手", "艺人", "音乐人"]},
    "album":         {"cn": "专辑", "aliases": ["唱片", "碟"]},
    "track":         {"cn": "曲目", "aliases": ["歌曲", "单曲", "音乐"]},
    "genre":         {"cn": "流派", "aliases": ["音乐类型", "曲风"]},
    "media_type":    {"cn": "媒体类型", "aliases": ["文件格式"]},
    "customer":      {"cn": "客户", "aliases": ["顾客", "买家"]},
    "employee":      {"cn": "员工", "aliases": ["雇员", "客服"]},
    "invoice":       {"cn": "账单", "aliases": ["发票", "订单", "购买记录"]},
    "invoice_line":  {"cn": "账单明细", "aliases": ["订单明细", "购买明细"]},
    "playlist":      {"cn": "播放列表", "aliases": ["歌单"]},
    "playlist_track": {"cn": "播放列表曲目", "aliases": []},
}

CHINOOK_COLUMNS = {
    ("artist", "artist_id"): {"cn": "艺术家ID", "role": "id"},
    ("artist", "name"): {"cn": "艺术家名称", "role": "dimension", "aliases": ["歌手名"]},
    ("album", "album_id"): {"cn": "专辑ID", "role": "id"},
    ("album", "title"): {"cn": "专辑名", "role": "dimension", "aliases": ["专辑名称"]},
    ("album", "artist_id"): {"cn": "所属艺术家", "role": "dimension"},
    ("track", "track_id"): {"cn": "曲目ID", "role": "id"},
    ("track", "name"): {"cn": "曲目名", "role": "dimension", "aliases": ["歌名", "歌曲名"]},
    ("track", "album_id"): {"cn": "所属专辑", "role": "dimension"},
    ("track", "media_type_id"): {"cn": "媒体类型", "role": "dimension"},
    ("track", "genre_id"): {"cn": "流派", "role": "dimension"},
    ("track", "composer"): {"cn": "作曲", "role": "dimension", "aliases": ["词曲作者"]},
    ("track", "milliseconds"): {"cn": "时长(毫秒)", "role": "metric", "unit": "ms", "aliases": ["时长"]},
    ("track", "bytes"): {"cn": "文件大小(字节)", "role": "metric", "unit": "B"},
    ("track", "unit_price"): {"cn": "单价", "role": "metric", "unit": "USD"},
    ("genre", "genre_id"): {"cn": "流派ID", "role": "id"},
    ("genre", "name"): {"cn": "流派名称", "role": "dimension"},
    ("media_type", "media_type_id"): {"cn": "媒体类型ID", "role": "id"},
    ("media_type", "name"): {"cn": "媒体类型名称", "role": "dimension"},
    ("customer", "customer_id"): {"cn": "客户ID", "role": "id"},
    ("customer", "first_name"): {"cn": "名", "role": "dimension"},
    ("customer", "last_name"): {"cn": "姓", "role": "dimension"},
    ("customer", "company"): {"cn": "公司", "role": "dimension"},
    ("customer", "country"): {"cn": "国家", "role": "dimension", "aliases": ["所在国家"]},
    ("customer", "email"): {"cn": "邮箱", "role": "text", "sens": "restricted"},
    ("customer", "support_rep_id"): {"cn": "客服代表", "role": "dimension"},
    ("employee", "employee_id"): {"cn": "员工ID", "role": "id"},
    ("employee", "first_name"): {"cn": "员工名", "role": "dimension"},
    ("employee", "last_name"): {"cn": "员工姓", "role": "dimension"},
    ("employee", "title"): {"cn": "职位", "role": "dimension"},
    ("employee", "reports_to"): {"cn": "汇报对象", "role": "dimension"},
    ("invoice", "invoice_id"): {"cn": "账单ID", "role": "id"},
    ("invoice", "customer_id"): {"cn": "客户", "role": "dimension"},
    ("invoice", "invoice_date"): {"cn": "账单日期", "role": "time", "aliases": ["购买日期", "下单时间"]},
    ("invoice", "billing_country"): {"cn": "账单国家", "role": "dimension", "aliases": ["国家"]},
    ("invoice", "total"): {"cn": "账单总额", "role": "metric", "unit": "USD", "aliases": ["金额", "消费金额", "总额"]},
    ("invoice_line", "invoice_line_id"): {"cn": "明细ID", "role": "id"},
    ("invoice_line", "invoice_id"): {"cn": "所属账单", "role": "dimension"},
    ("invoice_line", "track_id"): {"cn": "曲目", "role": "dimension"},
    ("invoice_line", "unit_price"): {"cn": "单价", "role": "metric", "unit": "USD"},
    ("invoice_line", "quantity"): {"cn": "数量", "role": "metric", "unit": "首"},
    ("playlist", "playlist_id"): {"cn": "播放列表ID", "role": "id"},
    ("playlist", "name"): {"cn": "播放列表名", "role": "dimension", "aliases": ["歌单名"]},
    ("playlist_track", "playlist_id"): {"cn": "播放列表", "role": "dimension"},
    ("playlist_track", "track_id"): {"cn": "曲目", "role": "dimension"},
}

CHINOOK_RELATIONS = [
    ("album", ["artist_id"], "artist", ["artist_id"], "n:1", "foreign_key", True, "专辑归属艺术家"),
    ("track", ["album_id"], "album", ["album_id"], "n:1", "foreign_key", True, "曲目归属专辑"),
    ("track", ["genre_id"], "genre", ["genre_id"], "n:1", "foreign_key", True, "曲目归属流派"),
    ("track", ["media_type_id"], "media_type", ["media_type_id"], "n:1", "foreign_key", True, "曲目的媒体类型"),
    ("invoice", ["customer_id"], "customer", ["customer_id"], "n:1", "foreign_key", True, "账单归属客户"),
    ("customer", ["support_rep_id"], "employee", ["employee_id"], "n:1", "foreign_key", True, "客户的客服代表"),
    ("invoice_line", ["invoice_id"], "invoice", ["invoice_id"], "n:1", "foreign_key", True, "明细归属账单"),
    ("invoice_line", ["track_id"], "track", ["track_id"], "n:1", "foreign_key", True, "明细对应曲目"),
    ("playlist_track", ["playlist_id"], "playlist", ["playlist_id"], "n:1", "foreign_key", True, "歌单曲目关联"),
    ("playlist_track", ["track_id"], "track", ["track_id"], "n:1", "foreign_key", True, "歌单曲目关联"),
    ("track", ["composer"], "artist", ["name"], "n:1", "business", False,
     "作曲与艺术家同名（弱关系，未经审核，禁止自动 JOIN）"),
]

CHINOOK_METRICS = [
    ("revenue", "销售收入", ["销售额", "营收", "消费金额"],
     "invoice.total 之和（账单粒度）",
     "sum(i.total)", "USD", "账单"),
    ("tracks_sold", "售出曲目数", ["销量"],
     "sum(invoice_line.quantity)",
     "sum(il.quantity)", "首", "账单明细"),
]


def md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()[:12]


def introspect(cur, db_name):
    """从 pg_catalog 读取表/列/主外键"""
    cur.execute("""
        SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod),
               a.attnum, a.attnotnull,
               EXISTS (SELECT 1 FROM pg_constraint pk
                       WHERE pk.conrelid = c.oid AND pk.contype='p' AND a.attnum = ANY(pk.conkey))
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
        WHERE n.nspname = 'public' AND c.relkind = 'r'
        ORDER BY c.relname, a.attnum""")
    cols = cur.fetchall()
    cur.execute("""
        SELECT tc.relname,
               (SELECT array_agg(a2.attname ORDER BY a2.attnum)
                  FROM unnest(k.conkey) kn JOIN pg_attribute a2
                    ON a2.attrelid = k.conrelid AND a2.attnum = kn),
               ft.relname,
               (SELECT array_agg(a3.attname ORDER BY a3.attnum)
                  FROM unnest(k.confkey) fn JOIN pg_attribute a3
                    ON a3.attrelid = k.confrelid AND a3.attnum = fn)
        FROM pg_constraint k
        JOIN pg_class tc ON tc.oid = k.conrelid
        JOIN pg_class ft ON ft.oid = k.confrelid
        JOIN pg_namespace n ON n.oid = tc.relnamespace
        WHERE k.contype = 'f' AND n.nspname = 'public'""")
    fks = cur.fetchall()
    return cols, fks


def main():
    meta = psycopg2.connect(dbname="datamind_meta", **PG)
    mcur = meta.cursor()

    jobs = [
        ("demo_retail", RETAIL_TABLES, RETAIL_COLUMNS, RETAIL_RELATIONS, RETAIL_METRICS, RETAIL_ALIASES),
        ("chinook", CHINOOK_TABLES, CHINOOK_COLUMNS, CHINOOK_RELATIONS, CHINOOK_METRICS, []),
    ]

    for db_name, tbl_dict, col_dict, rels, metrics, aliases in jobs:
        src = psycopg2.connect(dbname=db_name, **PG)
        scur = src.cursor()
        cols, fks = introspect(scur, db_name)
        src.close()

        tables = sorted({r[0] for r in cols})
        snapshot_id = f"snap-{db_name}-{md5(db_name + SNAPSHOT_TIME.isoformat())}"
        mcur.execute("SELECT COALESCE(MAX(catalog_version), 0) + 1 FROM schema_snapshots WHERE source_id=%s",
                     (db_name,))
        catalog_version = mcur.fetchone()[0]

        # data_sources
        mcur.execute("""
            INSERT INTO data_sources(source_id, name, db_type, host, port, database_name,
                                     schema_allowlist, access_mode, environment, description)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (source_id) DO UPDATE SET updated_at = now()""",
            (db_name, f"{db_name} 演示数据源", "postgresql", "postgres", 5432, db_name,
             ["public"], "readonly", "demo",
             "DataMind Agent 结构化演示数据源（只读）"))

        # snapshot
        mcur.execute("""
            INSERT INTO schema_snapshots(snapshot_id, source_id, catalog_version, table_count, column_count, notes)
            VALUES (%s,%s,%s,%s,%s,%s)""",
            (snapshot_id, db_name, catalog_version, len(tables), len(cols),
             f"introspected {SNAPSHOT_TIME.isoformat()}"))

        # tables
        for t in tables:
            info = tbl_dict.get(t, {})
            mcur.execute("""
                INSERT INTO schema_tables(snapshot_id, table_name, business_name, aliases)
                VALUES (%s,%s,%s,%s)""",
                (snapshot_id, t, info.get("cn", t), info.get("aliases", [])))

        # columns
        for t, c, dtype, attnum, notnull, ispk in cols:
            info = col_dict.get((t, c), {})
            mcur.execute("""
                INSERT INTO schema_columns(snapshot_id, schema_name, table_name, column_name,
                    ordinal, data_type, is_nullable, is_primary_key, business_name,
                    aliases, unit, semantic_role, enum_map, sensitivity)
                VALUES (%s,'public',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (snapshot_id, t, c, attnum, dtype, not notnull, ispk,
                 info.get("cn", c), info.get("aliases", []), info.get("unit"),
                 info.get("role"), json.dumps(info["enum"], ensure_ascii=False) if info.get("enum") else None,
                 info.get("sens", "public")))

        # relations（人工审核过的字典为准，introspection 的 FK 与字典去重后登记）
        seen = set()
        for rel in rels:
            ft, fc, tt, tc, card, kind, approved, desc = rel
            seen.add((ft, tuple(fc), tt))
            mcur.execute("""
                INSERT INTO schema_relations(relation_id, snapshot_id, from_table, from_columns,
                    to_table, to_columns, cardinality, relation_kind, approved, description)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (f"rel-{db_name}-v{catalog_version}-{md5(str(rel))}", snapshot_id, ft, fc, tt, tc, card, kind, approved, desc))
        for ft, fcols, tt, tcols in fks:
            if (ft, tuple(fcols), tt) not in seen:
                mcur.execute("""
                    INSERT INTO schema_relations(relation_id, snapshot_id, from_table, from_columns,
                        to_table, to_columns, cardinality, relation_kind, approved, description)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (f"rel-{db_name}-v{catalog_version}-{md5(ft+str(fcols)+tt)}", snapshot_id, ft, fcols, tt, tcols,
                     "n:1", "foreign_key", True, "introspection 外键（自动登记）"))

        # business_metrics
        for mid, name, al, definition, expr, unit, grain in metrics:
            mcur.execute("""
                INSERT INTO business_metrics(metric_id, name, aliases, definition, expression_sql,
                                             unit, grain, source_id, status)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'active')
                ON CONFLICT (metric_id) DO UPDATE SET
                    name=EXCLUDED.name, aliases=EXCLUDED.aliases, definition=EXCLUDED.definition,
                    expression_sql=EXCLUDED.expression_sql""",
                (f"{db_name}.{mid}", name, al, definition, expr, unit, grain, db_name))

        # entity_aliases
        for cid, cname, etype, alias in aliases:
            mcur.execute("""
                INSERT INTO entity_aliases(alias_id, canonical_id, canonical_name, entity_type,
                                           alias, source, match_method, approved)
                VALUES (%s,%s,%s,%s,%s,'seed','exact',TRUE)
                ON CONFLICT (alias_id) DO NOTHING""",
                (f"al-{md5(cid+alias)}", cid, cname, etype, alias))

        print(f"[{db_name}] tables={len(tables)} cols={len(cols)} "
              f"relations_dict={len(rels)} fk_extra={len(fks)} metrics={len(metrics)}")

    meta.commit()

    # 汇总
    mcur.execute("""
        SELECT ds.source_id, s.table_count, s.column_count,
               (SELECT count(*) FROM schema_relations r WHERE r.snapshot_id = s.snapshot_id) rels,
               (SELECT count(*) FROM business_metrics bm WHERE bm.source_id = ds.source_id) metrics
        FROM data_sources ds JOIN schema_snapshots s ON s.source_id = ds.source_id
        WHERE s.catalog_version = (SELECT MAX(s2.catalog_version) FROM schema_snapshots s2
                                   WHERE s2.source_id = ds.source_id)""")
    for row in mcur.fetchall():
        print("catalog:", row)
    mcur.execute("SELECT count(*) FROM entity_aliases")
    print("entity_aliases:", mcur.fetchone()[0])
    meta.close()


if __name__ == "__main__":
    main()
