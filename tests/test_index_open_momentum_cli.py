"""CLI 参数与编排 —— 计划 Task 7 step 4。

只测**参数面**：解析、校验、以及"哪些组合必须当场拒绝"。真去库里取那半由
`common/minute/pg_source` 的测试家族与 `docs/research/2026-08-27-cffex-session-crosscheck.md`
里的 EXPLAIN 冒烟负责，不在这里造假连接。
"""

from datetime import date

import pytest

from index_open_momentum.__main__ import build_parser, resolve_options


def _parse(*argv):
    if "--output-prefix" not in argv:
        argv = (*argv, "--output-prefix", "runs/index-open-momentum")
    return resolve_options(build_parser().parse_args(argv))


def _base(*extra):
    return _parse(
        "--start",
        "2016-01-04",
        "--end",
        "2016-12-30",
        "--output-prefix",
        "runs/index-open-momentum",
        *extra,
    )


# --------------------------------------------------------------------------
# 1. 起止日期
# --------------------------------------------------------------------------


def test_start_and_end_are_parsed_as_dates():
    options = _base()

    assert options.start == date(2016, 1, 4)
    assert options.end == date(2016, 12, 30)


def test_an_end_before_the_start_is_rejected():
    with pytest.raises(SystemExit):
        _parse("--start", "2016-12-30", "--end", "2016-01-04")


def test_a_start_before_the_if_listing_day_is_rejected():
    """IF 2010-04-16 挂牌，之前没有任何股指期货分钟数据。"""
    with pytest.raises(SystemExit):
        _parse("--start", "2009-01-05", "--end", "2016-12-30")


# --------------------------------------------------------------------------
# 2. 品种
# --------------------------------------------------------------------------


def test_the_paper_universe_is_the_default():
    """研报口径是 IF / IC / IH —— IM 在研报之后才上市，默认不进。"""
    assert _base().products == ("IF", "IC", "IH")


def test_products_can_be_narrowed():
    assert _base("--products", "IF").products == ("IF",)


def test_an_unknown_product_is_rejected():
    with pytest.raises(SystemExit):
        _base("--products", "RB")


def test_im_can_be_asked_for_explicitly_but_is_not_paper_faithful():
    """IM 可以跑，但它不在研报样本里 —— 结果不许标 paper-faithful。"""
    options = _base("--products", "IF,IM")

    assert options.products == ("IF", "IM")
    assert options.paper_faithful_possible is False


# --------------------------------------------------------------------------
# 3. 时段版本
# --------------------------------------------------------------------------


def test_the_session_version_defaults_to_cffex_v1():
    assert _base().session_version == "cffex-v1"


def test_an_unregistered_session_version_is_rejected():
    with pytest.raises(SystemExit):
        _base("--session-version", "commodity-v1")


# --------------------------------------------------------------------------
# 4. paper-faithful 闸
# --------------------------------------------------------------------------


def test_requiring_paper_faithful_over_a_window_that_cannot_be_is_rejected():
    """2016 前缺 15:00–15:15 那截是已登记的档案缺口，硬要 paper-faithful 就该当场拒。

    ⚠️ 拒的是**这个组合**，不是那段数据：区间落在 2016 之后照样可以硬要。
    """
    with pytest.raises(SystemExit):
        _parse(
            "--start", "2011-06-01", "--end", "2015-12-31", "--require-paper-faithful"
        )


def test_requiring_paper_faithful_after_2016_is_allowed():
    options = _parse(
        "--start", "2016-01-04", "--end", "2020-12-31", "--require-paper-faithful"
    )

    assert options.require_paper_faithful is True


def test_the_gate_is_off_by_default_but_the_window_is_still_flagged():
    """默认不强制，但"这段区间能不能标 paper-faithful"这个事实照样要算出来。"""
    options = _parse("--start", "2011-06-01", "--end", "2015-12-31")

    assert options.require_paper_faithful is False
    assert options.paper_faithful_possible is False


# --------------------------------------------------------------------------
# 5. 输出
# --------------------------------------------------------------------------


def test_an_output_prefix_is_required():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--start", "2016-01-04", "--end", "2016-12-30"])


def test_the_output_prefix_is_kept_verbatim():
    assert _base().output_prefix == "runs/index-open-momentum"


# --------------------------------------------------------------------------
# 6. 取数上界
# --------------------------------------------------------------------------


def test_a_window_past_the_minute_table_is_rejected():
    """`futures_minute` 最新 bar 是 2026-08-11。越过它就是在要不存在的数据。"""
    with pytest.raises(SystemExit):
        _parse("--start", "2026-01-05", "--end", "2026-12-31")


def test_the_tail_beyond_the_daily_table_switches_the_dominant_source():
    """`futures_daily` 连续覆盖只到 2026-04-29；其后主力要改从分钟表推。"""
    options = _parse("--start", "2026-05-06", "--end", "2026-08-11")

    assert options.dominant_source == "minute"


def test_a_window_inside_the_daily_table_uses_the_daily_source():
    assert _base().dominant_source == "daily"


def test_a_window_straddling_the_daily_boundary_is_rejected():
    """跨界要两条通路各跑一段并拼起来 —— 静默拼接会让换月规则在中间悄悄变一次。"""
    with pytest.raises(SystemExit):
        _parse("--start", "2026-01-05", "--end", "2026-08-11")
