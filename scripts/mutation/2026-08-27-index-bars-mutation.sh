#!/usr/bin/env bash
set -u
cd /home/elfbob/claude-code/futures_strategies
BARS=index_open_momentum/bars.py
BT=index_open_momentum/backtest.py
SIG=index_open_momentum/signals.py
BK=$(mktemp -d); cp $BARS $BK/b; cp $BT $BK/t; cp $SIG $BK/s
restore(){ cp $BK/b $BARS; cp $BK/t $BT; cp $BK/s $SIG; }
trap restore EXIT

py(){ python3 -c "
import pathlib,sys
p=pathlib.Path(sys.argv[1]); t=p.read_text(encoding='utf-8')
old,new=sys.argv[2],sys.argv[3]
assert old in t, 'PATTERN NOT FOUND: '+old[:70]
p.write_text(t.replace(old,new,1),encoding='utf-8')
" "$@"; }

run(){
  local label="$1" out failed n
  # ⚠️ 等长变异 + 同秒 mtime 会让 .pyc 不失效 —— 见记忆 mutation-runs-need-pycache-clear
  find common index_open_momentum tests -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null
  out=$(.venv/bin/python -m pytest tests/test_index_open_momentum_bars.py tests/test_index_open_momentum_backtest.py tests/test_index_open_momentum_signals.py -q -p no:cacheprovider 2>&1)
  failed=$(printf '%s\n' "$out" | grep -oE '^FAILED [^ ]+::[A-Za-z0-9_]+' | sed 's/.*:://' | sort -u | tr '\n' ' ')
  n=$(printf '%s' "$failed" | wc -w)
  if printf '%s\n' "$out" | grep -q "error during collection\|^ERROR "; then
    echo "  ✅ $label —— 模块无法加载"
  elif [ "$n" -eq 0 ]; then
    echo "  ❌ $label —— 没有任何用例变红"
  else
    echo "  ✅ $label —— 打红 $n 个: $failed"
  fi
  restore
}

echo "=== 基线 ==="
.venv/bin/python -m pytest tests/test_index_open_momentum_bars.py tests/test_index_open_momentum_backtest.py tests/test_index_open_momentum_signals.py -q 2>&1 | tail -1

echo; echo "=== A. 价域闸 ==="
py $BARS "MAX_RELATIVE_EXCURSION = 1e-6" "MAX_RELATIVE_EXCURSION = 1e-4"
run "N1 容差放回共享层的 1e-4"

py $BARS "    if fill.low == fill.high:" "    if False:"
run "N2 取消零宽窗口豁免"

py $BARS "    if fill.low == fill.high:" "    if fill.low <= fill.high:"
run "N3 零宽豁免扩成"永远豁免""

py $BARS "    if excursion > max_relative_excursion:" "    if excursion > 1e9:"
run "N4 紧闸形同虚设"

py $BARS "    return max(0.0, fill.low - raw, raw - fill.high) / scale" "    return 0.0"
run "N5 越界度量恒返回 0"

echo; echo "=== B. 乘数分流 ==="
py $BARS "    if metadata_multiplier is None:
        return infer_contract_multiplier(frame, contract=contract)" "    if True:
        return infer_contract_multiplier(frame, contract=contract)"
run "N6 忽略元数据，一律反推"

py $BARS "    return validate_metadata_multiplier(
        frame,
        contract=contract,
        multiplier=metadata_multiplier,
    )" "    from common.minute.bars import MultiplierResolution as _M
    return _M(multiplier=metadata_multiplier, source='metadata', sample_rows=0, pass_rate=1.0, sample_dates=0)"
run "N7 元数据不过价域校验，直接采信"

echo; echo "=== C. 15 分钟 K 线 ==="
py $BARS "                    None
                    if aggregated.no_trade" "                    Bar(open=0.0, high=0.0, low=0.0, close=0.0)
                    if aggregated.no_trade"
run "N8 no-trade 用一组 0 价冒充，而不是 None"

echo; echo "=== D. 持仓路径的 no-trade 语义 ==="
py $BT "            run_start = i + 1
            continue" "            continue"
run "N9 no-trade 不打断反向信号连续计数（透明跳过）"

py $BT "            bars[run_start : i + 1]," "            bars[: i + 1],"
run "N10 止损仍拿全前缀判定（等价于从不打断）"

py $BT "    for index in range(len(bars) - 1, after, -1):" "    for index in range(len(bars) - 1, -1, -1):"
run "N11 收盘平仓可退到入场那根（回看）"

py $BT "            traded_last = _last_traded_index(bars, after=entry_index)" "            traded_last = len(bars) - 1"
run "N12 收盘平仓直接用最后一根，不问有没有成交"

py $SIG "        b is not None and b.is_valid() for b in opening" "        b.is_valid() if b is not None else True for b in opening"
run "N13 开盘三根里的 None 被当成有效"

echo; echo "=== 收尾 ==="
.venv/bin/python -m pytest tests/test_index_open_momentum_bars.py tests/test_index_open_momentum_backtest.py tests/test_index_open_momentum_signals.py -q 2>&1 | tail -1
