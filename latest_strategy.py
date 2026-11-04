"""Latest research strategy. 65% is an acceptance target, never a guarantee."""
from bisect import bisect_left
from strategy import MINUTE, analyze, wilson_lower, validate
from reversion_rules import reversion_features
from point_research import point_backtest

POLICY='latest_v4'
VERSION='reversion_v4.0'
NAME='均值偏离后的确认反转'
TARGET=.65
MIN_SAMPLES=100
# Rules fixed before this point; all previously inspected data is research only.
VALIDATION_START_MS=1793783400000  # 2026-11-04 09:10:00 UTC


def latest_features(candles):
    return [{**row,'strategy_version':VERSION,'warmup_minutes':60,'setup':'带外回归确认' if row['qualified'] else '等待偏离后确认'}
            for row in reversion_features(candles,'band_reentry')]


def validation_from_rows(rows,direction,payout,unverified=0):
    """Research criterion, not a certified event-contract probability.

    Caller rows are generated below from the frozen rule and post-freeze price
    window. No user-supplied success flag is used to authorize the calculation.
    """
    required=max(TARGET,1/(1+payout)+.02)
    by_direction={}
    for selected in ('UP','DOWN'):
        subset=[r for r in rows if r['direction']==selected]
        wins=sum(r['outcome']=='WIN' for r in subset);losses=sum(r['outcome']=='LOSS' for r in subset)
        ties=sum(r['outcome']=='TIE' for r in subset);n=wins+losses
        lower=wilson_lower(wins,n)
        by_direction[selected]=dict(total=len(subset),decisive=n,wins=wins,losses=losses,ties=ties,
            observed_pct=round(100*wins/n,2) if n else None,lower_pct=round(100*lower,2) if n else None,
            passed=n>=MIN_SAMPLES and lower>=required)
    selected=by_direction.get(direction,dict(total=0,decisive=0,wins=0,losses=0,ties=0,observed_pct=None,lower_pct=None,passed=False))
    if direction not in by_direction:reason=f"当前没有方向触发；冻结规则后的研究样本：看涨{by_direction['UP']['decisive']}/{MIN_SAMPLES}，看跌{by_direction['DOWN']['decisive']}/{MIN_SAMPLES}"
    elif selected['decisive']<MIN_SAMPLES:reason=f"当前方向冻结规则后的完整新样本 {selected['decisive']}/{MIN_SAMPLES}，尚未达到65%研究验收要求"
    elif not selected['passed']:reason=f"当前方向95% Wilson研究下界未达到{100*required:.2f}%，暂停方向推送"
    else:reason='当前方向通过新时段分钟研究门槛；不代表实际订单或未来胜率保证'
    return {**selected,'target_pct':65,'min_samples':MIN_SAMPLES,'lower_required_pct':round(100*required,2),
            'validation_start':VALIDATION_START_MS,'by_direction':by_direction,'unverified':unverified,
            'reason':reason,'source':'post_freeze_minute_open_reference','actual_order_results':False,
            'guaranteed_win_rate':False,'note':'分钟研究门槛不是实单胜率；连续滚动统计和相关样本也不能保证未来65%。'}


def analyze_latest(candles,now_ms,payout=.8,demo=False,price_kind='spot',quote=None):
    validate(candles)
    features=latest_features(candles)
    result=analyze(candles,now_ms,payout,demo,price_kind,quote,policy='candidate',
                   feature_builder=lambda unused:features,history_builder=point_backtest)
    current=result['metrics']
    closed=[c.time+MINUTE for c in candles]
    start=bisect_left(closed,VALIDATION_START_MS)
    if start<len(candles):
        new_study=point_backtest(candles,features,start,len(candles),payout,keep_all=True)
        rows=new_study['trades'];unverified=new_study['unverified']
    else:rows=[];unverified=0
    target=validation_from_rows(rows,current['direction'],payout,unverified)
    def block(code,message):
        if code not in result['blocker_codes']:
            result['blocker_codes'].append(code);result['blockers'].append(message)
        result['action']='WAIT'
    if not 0<=result['data_age_seconds']<60:block('latest_stale','信号超过收盘后60秒，取消本次方向提醒')
    if quote and current['atr']>0 and abs(quote['price']-candles[-1].close)>.5*current['atr']:
        block('latest_price_moved','指数偏离信号收盘价超过0.5 ATR，取消追价')
    if not target['passed']:block('target_not_validated',target['reason'])
    if demo or price_kind!='index':target={**target,'passed':False,'reason':'只有真实币安指数可进入研究验收；演示和其它来源不发送正式方向'}
    metrics=[dict(label='RSI14',value=round(current['rsi'],2)),
             dict(label='ATR / 价格',value=f"{current['atr_pct']:.4f}%"),
             dict(label='20分钟方向效率',value=round(current['efficiency_20'],4) if current['efficiency_20'] is not None else '预热中'),
             dict(label='收盘在K线中的位置',value=f"{100*current['close_location']:.1f}%")]
    result.update(policy=POLICY,strategy_version=VERSION,strategy_name=NAME,research_validation=target,
        display_metrics=metrics,guaranteed_win_rate=False,advantage_verified=False,
        reference_study=dict(early=result['early'],holdout=result['holdout']),
        freshness_limit_seconds=60,valid_until=closed[-1]+60000,setup=current['setup'],
        signal_close=candles[-1].close,signal_reasons=current['reasons'],
        warnings=['65%是研究验收目标，不是已承诺或已证实的未来胜率；实际订单仍以币安记录为准'])
    result['diagnostics'].update(gate_sample_required=MIN_SAMPLES,gate_lower_required_pct=round(100*max(TARGET,1/(1+payout)+.02),2),
        gate_passed=target['passed'],validation='仅用冻结规则之后的新分钟研究检查65%目标，未达标等待；旧数据只做研究')
    return result
