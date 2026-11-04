"""Frozen price-feature consensus; no count quota or historical hit-rate gate."""
from bisect import bisect_left
from functools import lru_cache
from strategy import MINUTE, analyze, validate
from point_research import point_backtest
from consensus_inference import build_features, load_bundle

POLICY='latest_v6'
VERSION='precision_v6.0'
NAME='多周期指数共识研究'


@lru_cache(maxsize=2)
def _feature_cache(candles):
    return tuple({**row,'strategy_version':VERSION} for row in build_features(candles))


def latest_features(candles):
    return list(_feature_cache(tuple(candles)))


def output_study(candles,features,payout=.8,keep_all=False):
    """Only post-calibration outputs; training scores are never performance."""
    cutoff=load_bundle()['metadata']['calibration_end_exclusive']
    start=max(60,bisect_left([c.time+MINUTE for c in candles],cutoff))
    if start>=len(candles):return None
    return point_backtest(candles,features,start,len(candles),payout,keep_all=keep_all)


def analyze_latest(candles,now_ms,payout=.8,demo=False,price_kind='spot',quote=None):
    validate(candles)
    features=latest_features(candles)
    result=analyze(candles,now_ms,payout,demo,price_kind,quote,policy='candidate',
                   feature_builder=lambda unused:features,history_builder=point_backtest)
    current=result['metrics']
    if not 0<=result['data_age_seconds']<60:
        result['blocker_codes'].append('latest_stale')
        result['blockers'].append('信号超过收盘后60秒，取消本次方向提醒')
        result['action']='WAIT'
    study=output_study(candles,features,payout)
    win_rate=round(100*study['wins']/study['total'],2) if study and study['total'] else None
    closed=candles[-1].time+MINUTE
    strengths=current.get('model_strength_ratios',{})
    display=[dict(label='模型方向共识',value='同向' if current.get('model_agreement') else '未同向'),
             dict(label='模型一筛选强度 / 阈值',value=round(strengths.get('hgb',0),3)),
             dict(label='模型二筛选强度 / 阈值',value=round(strengths.get('extra_trees',0),3)),
             dict(label='ATR / 价格',value=f"{current['atr_pct']:.4f}%")]
    assessment=dict(status='research_only',test_samples=study['total'] if study else 0,
                    test_win_rate_pct=win_rate,
                    note='只统计训练与校准之后、策略实际选择的完整参考记录；筛选强度不是单笔成功率。历史结果不参与下一笔方向的准入。')
    result.update(policy=POLICY,strategy_version=VERSION,strategy_name=NAME,
        research_mode=True,quality_assessment=assessment,display_metrics=display,
        guaranteed_win_rate=False,advantage_verified=False,
        reference_study=dict(early=None,holdout=study),
        freshness_limit_seconds=60,valid_until=closed+MINUTE,setup=current['setup'],
        signal_close=candles[-1].close,signal_reasons=current['reasons'],
        reference_entry_time=closed+MINUTE,reference_expiry_time=closed+11*MINUTE,
        selection_diagnostics=dict(method='frozen_dual_model_price_consensus',
                                   score_is_win_probability=False,count_quota=None,
                                   past_hit_rate_permission_gate=False),
        warnings=['研究方向，准确率待持续验证；只有模型同向且两个方向强度都达冻结阈值才输出',
                  '不限制次数，也不设历史命中率准入门槛；不同意或信号弱时等待',
                  '分钟开盘参考实验不是你的实际订单；历史数据已多次检视，不是独立的未来业绩证明'])
    result['diagnostics'].update(gate_sample_required=None,gate_lower_required_pct=None,
        gate_passed=None,validation='只按当时已收盘指数特征、两个模型共识和冻结强度筛选；无历史胜率或次数门槛')
    return result
