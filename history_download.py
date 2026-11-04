"""Resumable public BTCUSDT index history. No credentials, pushes, or orders."""
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import urlencode
import uuid
from market import request, MarketError, FUTURES
from strategy import Candle, MINUTE, validate

SOURCE='binance_usdm_BTCUSDT_indexPriceKlines_1m'
PAGE_SIZE=500


def atomic_text(path, text):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix='.'+path.name+'-',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8',newline='') as stream:
            stream.write(text);stream.flush();os.fsync(stream.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)


def _json(value):return json.dumps(value,ensure_ascii=False,allow_nan=False,separators=(',',':'))


def _retry(path, progress, sleep):
    for attempt in range(4):
        try:return request(path,FUTURES,'币安指数',timeout=30)
        except MarketError as error:
            diagnostic=error.diagnostic
            transient=(diagnostic['code'] in ('network_error','incomplete_response','invalid_json') or
                       (diagnostic['code']=='http_error' and diagnostic['http_status'] in (408,500,502,503,504)))
            if not transient or attempt==3:raise
            delay=(1,3,7)[attempt]
            progress(f"连接暂时中断（{diagnostic['code']}），{delay}秒后重试；第{attempt+2}/4次请求。")
            sleep(delay)


def _page_candles(rows,start,end):
    if not isinstance(rows,list) or len(rows)!=(end-start)//MINUTE:raise ValueError('指数页数量不完整')
    output=[]
    for i,row in enumerate(rows):
        if not isinstance(row,list) or len(row)<5:raise ValueError('指数页字段不完整')
        try:
            stamp=int(row[0]);values=[float(x) for x in row[1:5]]
        except (ValueError,TypeError,OverflowError):raise ValueError('指数页数值无效') from None
        if isinstance(row[0],bool) or str(row[0])!=str(stamp) or stamp!=start+i*MINUTE:
            raise ValueError('指数页时间缺失、重复、乱序或越界')
        if not all(math.isfinite(x) and x>0 for x in values):raise ValueError('指数页价格无效')
        opening,high,low,close=values
        if low>min(opening,close) or high<max(opening,close) or low>high:raise ValueError('指数页高低价格无效')
        output.append(Candle(stamp,opening,high,low,close,0.0))
    return output


def _validate_manifest(manifest,count):
    if not isinstance(manifest,dict):raise ValueError('缓存清单不是对象')
    if manifest.get('schema')!=1 or manifest.get('source')!=SOURCE or manifest.get('count')!=count or manifest.get('page_size')!=PAGE_SIZE:
        raise ValueError('缓存清单来源或格式不匹配；使用--refresh创建新下载窗口')
    if not isinstance(manifest.get('snapshot_id'),str) or not re.fullmatch('[0-9a-f]{32}',manifest['snapshot_id']):
        raise ValueError('缓存清单编号无效')
    start,end=manifest.get('start'),manifest.get('end')
    if type(start) is not int or type(end) is not int or start<=0 or start%MINUTE or end%MINUTE or end-start!=count*MINUTE:
        raise ValueError('缓存清单时间窗口无效')
    if type(manifest.get('as_of')) is not int or not end<=manifest['as_of']<end+MINUTE:raise ValueError('缓存清单服务器时间无效')


def download_index_history(count,cache_dir,refresh=False,progress=print,sleep=time.sleep):
    """Return candles and metadata; resume freezes the existing window.

    Completed pages validate before reuse. A fully cached window requires no
    network, so failure of /time cannot block offline resume. --refresh creates
    a new snapshot, retaining old files. Only complete histories are returned.
    """
    if type(count) is not int or not 71<=count<=129600:raise ValueError('指数窗口须71分钟至90天')
    root=Path(cache_dir)/str(count);root.mkdir(parents=True,exist_ok=True)
    manifest_path=root/'manifest.json'
    if manifest_path.exists() and not refresh:
        try:manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
        except (ValueError,UnicodeError):raise ValueError('缓存清单损坏；使用--refresh创建新窗口，旧缓存仍保留') from None
        _validate_manifest(manifest,count)
        progress('继续上次下载，沿用原时间窗口；不会从头重下。')
    else:
        clock=_retry('/fapi/v1/time',progress,sleep)
        if not isinstance(clock,dict) or type(clock.get('serverTime')) is not int:raise ValueError('币安服务器时间响应无效')
        as_of=clock['serverTime'];end=as_of//MINUTE*MINUTE
        manifest=dict(schema=1,source=SOURCE,count=count,page_size=PAGE_SIZE,snapshot_id=uuid.uuid4().hex,
                      start=end-count*MINUTE,end=end,as_of=as_of)
        _validate_manifest(manifest,count)
        atomic_text(manifest_path,_json(manifest)+'\n')
    _validate_manifest(manifest,count)
    folder=root/manifest['snapshot_id'];folder.mkdir(exist_ok=True)
    iso=lambda stamp:datetime.fromtimestamp(stamp/1000,timezone.utc).isoformat()
    progress(f"指数窗口（UTC）：{iso(manifest['start'])} 至 {iso(manifest['end'])}；仅已收盘K线。")
    total=(count+PAGE_SIZE-1)//PAGE_SIZE;output=[];cached=downloaded=0
    for number,start in enumerate(range(manifest['start'],manifest['end'],PAGE_SIZE*MINUTE),1):
        end=min(manifest['end'],start+PAGE_SIZE*MINUTE);path=folder/f'{start}-{end}.json'
        page=None
        if path.exists():
            try:
                saved=json.loads(path.read_text(encoding='utf-8'))
                if not isinstance(saved,dict) or saved.get('source')!=SOURCE or saved.get('start')!=start or saved.get('end')!=end:
                    raise ValueError('缓存页来源或时间不匹配')
                rows=saved.get('rows')
                if saved.get('sha256')!=hashlib.sha256(_json(rows).encode()).hexdigest():raise ValueError('缓存页校验失败')
                page=_page_candles(rows,start,end)
            except (ValueError,UnicodeError,TypeError):
                os.replace(path,path.with_suffix('.invalid-'+str(time.time_ns())))
                progress(f'第{number}/{total}页缓存不完整，仅重新下载此页。')
        if page is not None:
            cached+=1;progress(f'第{number}/{total}页：使用已完成缓存。')
        else:
            progress(f'正在下载第{number}/{total}页（每页最多{PAGE_SIZE}根）…')
            query=urlencode(dict(pair='BTCUSDT',interval='1m',limit=PAGE_SIZE,startTime=start,endTime=end-1))
            rows=_retry('/fapi/v1/indexPriceKlines?'+query,progress,sleep)
            page=_page_candles(rows,start,end)
            # Cache normalized index OHLC only. Field 5 is Ignore, not volume.
            normalized=[[c.time,c.open,c.high,c.low,c.close] for c in page]
            saved=dict(source=SOURCE,start=start,end=end,rows=normalized,
                       sha256=hashlib.sha256(_json(normalized).encode()).hexdigest())
            atomic_text(path,_json(saved)+'\n')
            downloaded+=1;progress(f'第{number}/{total}页已保存，完成{len(output)+len(page)}/{count}根。')
            if end<manifest['end']:sleep(.2)
        output.extend(page)
    if len(output)!=count:raise ValueError('指数总数量不正确，不生成评估报告')
    validate(output)
    progress(f'下载完成：{count}根；复用{cached}页，新下载{downloaded}页。正在评估，请等待…')
    return output,manifest
