"""Stateless local review of user-supplied order points, not exchange verification."""
import csv
from decimal import Decimal, InvalidOperation
from io import StringIO

REQUIRED=('order_id','direction','open_time_ms','expiry_time_ms','open_index','close_index','net_payout','stake')


def number(value, label, low, high):
    try: result=Decimal(value)
    except (InvalidOperation,TypeError): raise ValueError(label+'不是有效数字') from None
    if not result.is_finite() or not low<=result<=high: raise ValueError(label+'超出范围')
    return result


def review_csv(text):
    if not isinstance(text,str) or len(text.encode('utf-8'))>1_000_000:raise ValueError('CSV须小于1MB')
    reader=csv.DictReader(StringIO(text.lstrip('\ufeff')))
    if reader.fieldnames is None or len(reader.fieldnames)!=len(set(reader.fieldnames)) or not set(REQUIRED)<=set(reader.fieldnames):
        raise ValueError('CSV列名须包含：'+','.join(REQUIRED))
    rows=[];seen=set();profit=Decimal(0);wins=losses=ties=unverified=0
    for index,row in enumerate(reader,2):
        if len(rows)>=5000:raise ValueError('单次最多5000行')
        if None in row or any(row.get(k) is None for k in REQUIRED):raise ValueError(f'第{index}行列数不正确')
        identifier=row['order_id'].strip()
        if not identifier or len(identifier)>120 or identifier in seen:raise ValueError(f'第{index}行订单编号为空或重复')
        seen.add(identifier)
        direction={'看涨':'UP','看跌':'DOWN'}.get(row['direction'].strip(),row['direction'].strip())
        if direction not in ('UP','DOWN'):raise ValueError(f'第{index}行方向须为UP或DOWN')
        try:
            opened=int(row['open_time_ms']);expiry=int(row['expiry_time_ms'])
        except ValueError:raise ValueError(f'第{index}行时间须为整数毫秒') from None
        if opened<=0 or expiry-opened!=600000:raise ValueError(f'第{index}行开仓至到期须为600秒')
        payout=number(row['net_payout'],'净收益比例',Decimal(0),Decimal('1000000'))
        stake=number(row['stake'],'本金',Decimal('.00000001'),Decimal('100000000'))
        entry=number(row['open_index'],'开仓指数',Decimal('.000000000001'),Decimal('1e12')) if row['open_index'].strip() else None
        exit_price=number(row['close_index'],'到期指数',Decimal('.000000000001'),Decimal('1e12')) if row['close_index'].strip() else None
        pnl=None
        if entry is None or exit_price is None:outcome='UNVERIFIED';unverified+=1
        elif entry==exit_price:outcome='TIE';ties+=1;pnl=Decimal(0)
        elif (exit_price>entry)==(direction=='UP'):outcome='WIN';wins+=1;pnl=stake*payout
        else:outcome='LOSS';losses+=1;pnl=-stake
        if pnl is not None:profit+=pnl
        rows.append(dict(order_id=identifier,direction=direction,open_time=opened,expiry=expiry,
                         entry=str(entry) if entry is not None else None,
                         exit=str(exit_price) if exit_price is not None else None,
                         outcome=outcome,profit=str(pnl) if pnl is not None else None))
    if not rows:raise ValueError('CSV没有订单记录')
    return dict(source='user_supplied_order_records',verified_by_exchange=False,
                note='仅按你导入的订单单点计算，未连接币安核验。缺失端点为未验证，不计胜负或盈亏；平价退款单列。',
                imported=len(rows),completed=wins+losses+ties,wins=wins,losses=losses,ties=ties,unverified=unverified,
                decisive_win_rate_pct=round(100*wins/(wins+losses),2) if wins+losses else None,
                win_rate_including_ties_pct=round(100*wins/(wins+losses+ties),2) if wins+losses+ties else None,
                profit_usdt=str(profit),trades=rows)
