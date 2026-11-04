"""Pure stdlib frozen consensus inference; scores are not validated win probabilities."""
import base64
import hashlib
import json
import math
from pathlib import Path
import struct
import zlib
from functools import lru_cache
from price_features import raw_features,feature_rows,FEATURE_NAMES,WARMUP

BUNDLE_NAME='consensus-models.b85'


@lru_cache(maxsize=1)
def load_bundle(path=None):
    try:
        payload=Path(path or Path(__file__).with_name(BUNDLE_NAME)).read_text(encoding='ascii')
        envelope=json.loads(zlib.decompress(base64.b85decode(payload.encode('ascii'))))
    except (OSError,UnicodeError,ValueError,zlib.error) as error:
        raise ValueError('固定模型文件缺失或损坏，请重新完整解压最新版') from error
    model_json=json.dumps(envelope['model'],ensure_ascii=True,separators=(',',':'),allow_nan=False)
    if hashlib.sha256(model_json.encode()).hexdigest()!=envelope['sha256']:
        raise ValueError('固定模型校验失败，请重新完整解压最新版')
    model=envelope['model']
    if model.get('schema')!=1 or tuple(model.get('feature_names',()))!=FEATURE_NAMES:
        raise ValueError('固定模型特征或格式不匹配，请重新完整解压最新版')
    model['_fingerprint']=envelope['sha256']
    _MODELS[model['_fingerprint']]=model
    return model


_MODELS={}


def _leaf(tree,values):
    i=0
    while True:
        # feature, threshold, left, right, leaf output (None for branches)
        feature,threshold,left,right,value=tree[i]
        if feature<0:
            return value
        i=left if values[feature]<=threshold else right


def _calculate_scores(values,model):
    if len(values)!=len(FEATURE_NAMES) or not all(math.isfinite(v) for v in values):
        raise ValueError('Invalid frozen model feature row')
    hgb=model['hgb'];logit=hgb['baseline']
    for tree in hgb['trees']:
        logit+=_leaf(tree,values)
    if logit>=0:
        exp=math.exp(-logit);a=1/(1+exp)
    else:
        exp=math.exp(logit);a=exp/(1+exp)
    # sklearn tree predictors consume IEEE float32 input even when features
    # were built in float64. Preserve the exact split convention on export.
    f32=[struct.unpack('f',struct.pack('f',v))[0] for v in values]
    b=sum(_leaf(tree,f32) for tree in model['extra_trees']['trees'])/len(model['extra_trees']['trees'])
    return a,b


@lru_cache(maxsize=65536)
def _cached_scores(values,fingerprint):
    return _calculate_scores(values,_MODELS[fingerprint])


def scores(values,model=None):
    model=load_bundle() if model is None else model
    first,second=_cached_scores(tuple(values),model['_fingerprint'])
    return {'hgb':first,'extra_trees':second}


def inference(candles,model=None):
    model=load_bundle() if model is None else model
    thresholds=model['thresholds']
    result=[]
    for values in raw_features(candles):
        if values is None:
            result.append(dict(qualified=False,direction=None,strength=None,agreement=False,high_strength=False))
            continue
        prediction=scores(values,model)
        directions={name:value>=.5 for name,value in prediction.items()}
        agreement=len(set(directions.values()))==1
        strong=all(abs(value-.5)>=thresholds[name] for name,value in prediction.items())
        direction=('UP' if directions['hgb'] else'DOWN') if agreement else None
        # Strength ratios summarize fixed feature scores only. They are not
        # empirical hit rates or the chance of this specific order succeeding.
        ratios={name:abs(value-.5)/thresholds[name] for name,value in prediction.items()}
        result.append(dict(qualified=bool(agreement and strong),direction=direction,
                           strength=min(ratios.values()),agreement=agreement,
                           high_strength=strong,model_scores=prediction,model_strength_ratios=ratios))
    return result


def build_features(candles):
    """Compatible production feature rows; no past-win/count permission gate.

    Training and calibration timestamps cannot be advertised as out-of-time
    signals. All model scores are feature-derived ordering values, never an
    assertion of this particular order's success probability.
    """
    model=load_bundle();thresholds=model['thresholds'];outputs=[]
    cutoff=model['metadata']['calibration_end_exclusive']
    for c,row in zip(candles,feature_rows(candles)):
        values=row.pop('values');history=values is not None
        prediction=scores(values,model) if history else {}
        sides=[p>=.5 for p in prediction.values()]
        agree=bool(sides and len(set(sides))==1)
        strengths={name:abs(value-.5)/thresholds[name] for name,value in prediction.items()}
        flags=(history,c.time+60000>=cutoff,agree,strengths.get('hgb',0)>=1,strengths.get('extra_trees',0)>=1)
        checks=[dict(code=code,label=label,passed=bool(passed)) for code,label,passed in zip(
            ('history120','after_calibration','model_agreement','model1_strength','model2_strength'),
            ('120根已收盘指数K线','处于模型训练与校准之后','两个固定模型方向一致','模型一方向分数达到冻结阈值','模型二方向分数达到冻结阈值'),flags)]
        direction=('UP' if sides[0] else 'DOWN') if agree else None
        qualified=bool(all(flags))
        outputs.append(dict(**row,direction=direction,qualified=qualified,checks=checks,
            failed_rules=[x['code'] for x in checks if not x['passed']],
            reasons=[x['label'] for x in checks if x['passed']],score=sum(flags),
            setup='双模型同向且强度共识' if qualified else '等待双模型同向强度共识',
            warmup_minutes=WARMUP,model_scores=prediction,model_strength_ratios=strengths,
            model_agreement=agree,research_only=True,
            score_is_win_probability=False))
    return outputs
