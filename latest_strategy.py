"""Latest research strategy. 65% is an acceptance target, never a guarantee."""
from bisect import bisect_left
from strategy import MINUTE, analyze, validate
from reversion_rules import reversion_features
from point_research import point_backtest
from frequency_research import study_frequency

POLICY='latest_v5'
VERSION='activity_v5.0'
NAME='ATR偏离后的反转研究'
TARGET=.65
MIN_SAMPLES=100
# Rules fixed before this point; all previously inspected data is research only.
VALIDATION_START_MS=1793787600000  # 2026-11-04 10:20:00 UTC


def latest_features(candles):
    return [{**row,'strategy_version':VERSION,'warmup_minutes':60,'setup':'ATR过度偏离后反转确认' if row['qualified'] else '等待ATR偏离后反转'}
            for row in reversion_features(candles,'atr_rejection')]


def validation_from_rows(rows,direction,payout,unverified=0):
    """Aggregate completed reference outcomes; no per-trade probability claim.

    65% means wins / (wins + losses + ties). Ties refund but are not wins.
    Direction breakdowns are diagnostic; neither direction is gated separately.
    Missing/pending endpoints stay explicit and never become losses or wins.
    """
    def summarize(subset):
        wins=sum(r['outcome']=='WIN' for r in subset)
        losses=sum(r['outcome']=='LOSS' for r in subset)
        ties=sum(r['outcome']=='TIE' for r in subset)
        if wins+losses+ties!=len(subset):raise ValueError('累计命中率只能计入已结算胜、负、平记录')
        total=len(subset)
        return dict(total=total,decisive=wins+losses,wins=wins,losses=losses,ties=ties,
                    observed_pct=round(100*wins/total,2) if total else None,
                    profit_units=round(wins*payout-losses,8))
    if any(r.get('direction') not in ('UP','DOWN') for r in rows):raise ValueError('研究记录方向无效')
    aggregate=summarize(rows)
    by_direction={side:summarize([r for r in rows if r['direction']==side]) for side in ('UP','DOWN')}
    rate_met=bool(aggregate['total'] and aggregate['wins']/aggregate['total']>=TARGET)
    sample_met=aggregate['total']>=MIN_SAMPLES
    profit_met=aggregate['profit_units']>0
    passed=sample_met and rate_met and profit_met
    if not sample_met:reason=f"冻结规则后的完整新样本 {aggregate['total']}/{MIN_SAMPLES}，累计命中率仅作进度，样本尚不足"
    elif not rate_met:reason=f"累计已结算命中率 {aggregate['observed_pct']:.2f}% 未达到65%研究目标"
    elif not profit_met:reason='累计命中率已达65%，但按当前净收益假设的研究净盈亏未为正'
    else:reason='累计已结算命中率达到65%研究目标，且假设净收益为正；不保证未来每批结果'
    return {**aggregate,'passed':passed,'target_pct':65,'min_samples':MIN_SAMPLES,
            'hit_rate_target_met':rate_met,'sample_requirement_met':sample_met,'profit_requirement_met':profit_met,
            'acceptance_metric':'wins_divided_by_all_completed_including_ties',
            'validation_start':VALIDATION_START_MS,'by_direction':by_direction,'unverified':unverified,
            'reason':reason,'source':'post_freeze_minute_open_reference','actual_order_results':False,
            'guaranteed_win_rate':False,'note':'65%指已完成的一批记录中获胜次数占比，不是单笔预测概率；分钟研究不等于实单，未验证另列。'}


def analyze_latest(candles,now_ms,payout=.8,demo=False,price_kind='spot',quote=None,research_mode=True):
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
    price_moved=bool(quote and current['atr']>0 and abs(quote['price']-candles[-1].close)>.5*current['atr'])
    if price_moved and not research_mode:
        block('latest_price_moved','指数偏离信号收盘价超过0.5 ATR，取消追价')
    # The user explicitly opted into research alerts below the 65% target.
    # Keep the honest aggregate measurement; never invent a per-trade chance.
    if not research_mode and not target['passed']:block('target_not_validated',target['reason'])
    if demo or price_kind!='index':target={**target,'passed':False,'reason':'只有真实币安指数可进入研究验收；演示和其它来源不发送正式方向'}
    metrics=[dict(label='RSI14',value=round(current['rsi'],2)),
             dict(label='ATR / 价格',value=f"{current['atr_pct']:.4f}%"),
             dict(label='20分钟方向效率',value=round(current['efficiency_20'],4) if current['efficiency_20'] is not None else '预热中'),
             dict(label='收盘在K线中的位置',value=f"{100*current['close_location']:.1f}%")]
    study=point_backtest(candles,features,60,len(candles),payout,keep_all=True)
    frequency=study_frequency(candles,study)
    result.update(policy=POLICY,strategy_version=VERSION,strategy_name=NAME,research_validation=target,
        research_mode=bool(research_mode),frequency_research=frequency,
        frequency_target=dict(window_hours=12,target=20,approximate=True,guaranteed=False),
        display_metrics=metrics,guaranteed_win_rate=False,advantage_verified=False,
        reference_study=dict(early=result['early'],holdout=result['holdout']),
        freshness_limit_seconds=60,valid_until=closed[-1]+60000,setup=current['setup'],
        signal_close=candles[-1].close,signal_reasons=current['reasons'],
        reference_entry_time=closed[-1]+MINUTE,reference_expiry_time=closed[-1]+11*MINUTE,
        warnings=['研究方向提醒：尚未证明达到65%累计命中率或盈利；65%指一批已结算记录中获胜的占比，不是单笔预测概率',
                  '每12小时约20次是频率目标，历史均值不能保证每个未来12小时都达到；缺少形态或行情过期仍不发送'])
    if price_moved and research_mode:
        result['warnings'].append('指数快照已偏离信号收盘价超过0.5 ATR；研究模式如实提示该变化，不把未回放的额外追价过滤冒充已验证条件')
    result['diagnostics'].update(gate_sample_required=MIN_SAMPLES,gate_lower_required_pct=None,
        gate_passed=target['passed'],research_alerts_enabled=bool(research_mode),
        validation='按冻结规则后的全部已结算研究记录检查累计65%目标；用户已选择未达目标也先接收研究提醒，仍检查形态和行情时效')
    return result
