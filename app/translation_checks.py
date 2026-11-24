"""Conservative numeric checks; uncertain ASR fragments remain visible for review."""
from __future__ import annotations
from collections import Counter
from decimal import Decimal, InvalidOperation
import re
import unicodedata

DIGITS=re.compile(r"(?<![A-Za-z0-9_])\d+(?:[.,]\d+)*(?![A-Za-z0-9_])")
QUANTITY=re.compile(r"^(?:[%％°℃]|年|月|日|時|時間|分|秒|歳|人|名|個|匹|枚|台|本|冊|円|ドル|回|番|階|点|度|倍|章|巻|話|件|組|種類|行|列|桁|ページ|キロ|メートル|センチ|グラム|パーセント|[+*/×÷=<>≤≥]|km\b|kg\b|cm\b|[GMK]B\b)",re.I)
QUANTITY_BEFORE=re.compile(r"第\s*$|(?:番号|数値|答え|結果|合計|約|最大|最小|マイナス|プラス)[^。！？\n]{0,6}$|[+*/×÷=<>≤≥-]\s*$")
NAME_AFTER=re.compile(r"^\s*[\u3400-\u9fff\u30a0-\u30ffA-Za-z][\u3040-\u30ff\u3400-\u9fffA-Za-z _-]{0,24}?(?:さん|様|さま|君|くん|ちゃん|氏)")
CN_NUMBER=re.compile(r"[零〇一二两三四五六七八九十百千万亿]+(?:点[零〇一二三四五六七八九]+)?")
CN_UNITS=re.compile(r"^(?:个|位|人|名|只|头|双|对|套|件|张|辆|台|支|根|颗|粒|瓶|杯|盒|包|份|间|家|所|座|区|元|岁|年|月|日|号|时|点|分|秒|步|次|倍|本|页|章|段|行|列|项|条|格|帧|丁目|种|米|厘米|千克|公斤|克|吨|天|周|小时|分钟|毫秒|美元|人民币|%|％)")
CN_DIGITS={c:i for i,c in enumerate('零一二三四五六七八九')};CN_DIGITS.update({'〇':0,'两':2})

class TranslationValidationError(ValueError):
    def __init__(self,identifier,problem):
        self.identifier=identifier;self.problem=problem
        super().__init__(f"第 {identifier} 段{problem}")

def canonical_number(value):
    value=unicodedata.normalize('NFKC',value).replace(',','')
    try:
        fixed=format(Decimal(value),'f')
        return fixed.rstrip('0').rstrip('.') if '.' in fixed else fixed
    except InvalidOperation:return value

def chinese_number(value):
    integer,_,fraction=value.partition('点')
    if all(c in CN_DIGITS for c in integer):
        number=int(''.join(str(CN_DIGITS[c]) for c in integer))
    else:
        total=section=digit=0
        for char in integer:
            if char in CN_DIGITS:digit=CN_DIGITS[char];continue
            unit={'十':10,'百':100,'千':1000,'万':10000,'亿':100000000}[char]
            if unit<10000:section+=(digit or 1)*unit
            elif unit==10000:total+=(section+digit)*unit;section=0
            else:total=(total+section+digit)*unit;section=0
            digit=0
        number=total+section+digit
    return canonical_number(str(number)+('.'+''.join(str(CN_DIGITS[c]) for c in fraction) if fraction else ''))

def numbers(text,*,written=False):
    text=unicodedata.normalize('NFKC',text)
    values=Counter(canonical_number(m.group()) for m in DIGITS.finditer(text))
    if written:
        for match in CN_NUMBER.finditer(text):
            before,after=text[:match.start()],text[match.end():]
            integer=match.group().partition('点')[0]
            # Bare digit sequences and abbreviated trailing places can be colloquial
            # ranges (二三) or quantities (一百二). Do not guess their numeric value.
            if len(integer)>1 and all(c in CN_DIGITS for c in integer) and not any(c in integer for c in '零〇'):continue
            if len(integer)>1 and integer[-1] in CN_DIGITS and integer[-2] in '百千万亿':continue
            # Do not interpret digits inside names or words (e.g. 八木/唯一).
            bounded=(not before or not before[-1].isalnum()) and (not after or not after[0].isalnum())
            if bounded or CN_UNITS.match(after) or (before.endswith(('第','是','为','有','共','达')) and (not after or not after[0].isalnum())):
                values[chinese_number(match.group())]+=1
    return values

# Hours only qualify as equivalent inside an explicit clock expression. Durations
# (20 hours), unqualified 8 o'clock, and unrelated quantities never imply 20:00.
TIME_DIGIT = r'(?:\d{1,2}|[零〇一二两三四五六七八九十]{1,3})'
TIME = re.compile(
    rf'(?<![\dA-Za-z])(?P<period>凌晨|早上|早晨|上午|中午|下午|傍晚|晚上|晚间|夜里|深夜|午前|午後)?\s*'
    rf'(?P<hour>{TIME_DIGIT})(?:(?:時(?!間)|时(?!间)|点(?!钟?后))(?:(?P<half>半)|\s*(?P<minute>{TIME_DIGIT})分?)?'
    rf'|[:：](?P<colon>\d{{2}})|\s*(?P<ampm>a\.?m\.?|p\.?m\.?))'
    rf'\s*(?P<suffix>a\.?m\.?|p\.?m\.?)?', re.I)


def clock_expressions(text):
    text=unicodedata.normalize('NFKC',text)
    clocks=[]
    for match in TIME.finditer(text):
        def value(raw):return int(raw) if raw.isdecimal() else int(chinese_number(raw))
        hour=value(match['hour']);minute=30 if match['half'] else value(match['minute'] or match['colon'] or '0')
        period=(match['period'] or match['ampm'] or match['suffix'] or '').lower().replace('.','')
        if not 0<=hour<=23 or not 0<=minute<=59:continue
        if period:
            if hour>12:continue
            if period in {'下午','傍晚','午後','pm'}:hour=hour%12+12
            elif period in {'晚上','晚间','夜里'}:hour=0 if hour==12 else hour+12
            elif period=='中午':
                if hour not in (11,12,1):continue
                hour=13 if hour==1 else hour
            elif period=='深夜':
                if hour==12:hour=0
                elif hour>5:continue
            elif period in {'凌晨','午前','am'}:hour=hour%12
            elif hour==12:continue  # Morning/noon wording is ambiguous.
        # Reject suffix seconds: the minute-only comparison cannot verify them.
        if re.match(r'\s*[:：]\d|\s*\d+秒',text[match.end():]):continue
        clocks.append((match,(hour,minute)))
    return text,clocks


def equivalent_clock_numbers(source,translated):
    source,clocks=clock_expressions(source)
    _,other=clock_expressions(translated)
    available=Counter(key for _,key in other);spans=[]
    for match,key in clocks:
        if available[key]:
            spans.append(match.span());available[key]-=1
    occurrences={}
    for match in DIGITS.finditer(source):
        occurrences.setdefault(canonical_number(match.group()),[]).append(match.span())
    return {value for value,parts in occurrences.items()
            if all(any(start<=left and right<=end for start,end in spans) for left,right in parts)}


# Recognize colloquial Chinese ranges only against two explicit source bounds
# with compatible units. A plain 23 never becomes "two or three"; a separate
# occurrence of either bound must still be preserved independently.
RANGE_UNITS={'年':'年','回':'次','次':'次','個':'个','人':'人','日':'天','天':'天',
             '月':'月','週間':'周','周':'周','コマ':'格','格':'格','帧':'格','時間':'小时'}
RANGE_UNIT='|'.join(sorted(RANGE_UNITS,key=len,reverse=True))
SOURCE_RANGE=re.compile(rf'(?<![\dA-Za-z])(?P<a>\d{{1,2}})\s*(?:(?P<u>{RANGE_UNIT})\s*(?:[、,，]|から|到|至|[~〜～—–-])?\s*|(?:到|至|から|[~〜～—–-])\s*)(?P<b>\d{{1,2}})\s*(?P<v>{RANGE_UNIT})')
TARGET_RANGE=re.compile(r'(?<![零〇一二两三四五六七八九十百千万亿\d])(?P<a>[一二两三四五六七八九])(?P<b>[一二两三四五六七八九])(?P<u>小时|年|次|个|人|天|月|周|格|帧)')

def equivalent_range_numbers(source,translated):
    source=unicodedata.normalize('NFKC',source)
    translated=unicodedata.normalize('NFKC',translated)
    available=Counter((CN_DIGITS[m['a']],CN_DIGITS[m['b']],RANGE_UNITS.get(m['u'],m['u'])) for m in TARGET_RANGE.finditer(translated))
    spans=[]
    for match in SOURCE_RANGE.finditer(source):
        a,b=int(match['a']),int(match['b']);unit=RANGE_UNITS[match['v']]
        if not (1<=a<b<=9 and b==a+1):continue
        if match['u'] and RANGE_UNITS[match['u']]!=unit:continue
        key=(a,b,unit)
        if available[key]:spans.append(match.span());available[key]-=1
    occurrences={}
    for match in DIGITS.finditer(source):occurrences.setdefault(canonical_number(match.group()),[]).append(match.span())
    return {value for value,parts in occurrences.items()
            if all(any(start<=left and right<=end for start,end in spans) for left,right in parts)}


def uncertain_source_numbers(text):
    text=unicodedata.normalize('NFKC',text);uncertain=Counter()
    for match in DIGITS.finditer(text):
        # A narrow uncertainty rule, not an ASR correction: a single digit spliced
        # between a Japanese word ending and an honorific/name, without quantity
        # markers. Preserve the source verbatim and always show a review warning.
        before,after=text[:match.start()],text[match.end():]
        if (len(match.group())==1 and before and '\u3040'<=before[-1]<='\u309f'
            and NAME_AFTER.match(after) and not QUANTITY.match(after.lstrip())
            and not QUANTITY_BEFORE.search(before[-18:])):
            uncertain[canonical_number(match.group())]+=1
    total=numbers(text)
    return {value for value,count in uncertain.items() if count==total[value]}

def required_numbers(text):return sorted(set(numbers(text))-uncertain_source_numbers(text))

def japanese_fragments(text):
    # Normalize half-width kana, but do not flag Japanese punctuation alone.
    return re.findall(r'[ぁ-ゖァ-ヺ][ぁ-ゖァ-ヺー]*',unicodedata.normalize('NFKC',text))

def language_issues(text,target='zh'):
    fragments=japanese_fragments(text) if target=='zh' else []
    return [dict(code='target_language_mismatch',fragments=list(dict.fromkeys(fragments))[:8],
        message='中文译文仍含日文假名；请重新翻译或润色，并核对人名与称呼。')] if fragments else []

def project_language_checks(project):
    """Derived warnings for existing translations; never changes saved drafts."""
    from .languages import target_code
    target=target_code(project);checks=dict(project.get('translation_checks',{}))
    for row in project['segments']:
        issues=language_issues(row.get('zh',''),target)
        if not issues:continue
        previous=checks.get(str(row['id']),{})
        valid=previous.get('source')==row['text'] and previous.get('translation')==row['zh'] and previous.get('target_language')==target
        checks[str(row['id'])]=dict(source=row['text'],translation=row['zh'],target_language=target,
            issues=[i for i in previous.get('issues',[]) if i.get('code')!='target_language_mismatch']+issues if valid else issues)
    return checks

def validate_values(rows,result,target_language='zh'):
    checks={}
    for row in rows:
        source=row['text'];translated=result[row['id']]['zh'];uncertain=uncertain_source_numbers(source)
        if language_issues(translated,target_language):
            raise TranslationValidationError(row['id'],'中文译文残留日文假名：'+', '.join(japanese_fragments(translated)[:8])+'；人名与称呼也应转换成中文表达')
        missing=set(numbers(source))-set(numbers(translated,written=True))-uncertain
        if missing:missing-=equivalent_clock_numbers(source,translated)
        if missing and target_language=='zh':missing-=equivalent_range_numbers(source,translated)
        if missing:raise TranslationValidationError(row['id'],f"缺失或改写数字：{', '.join(sorted(missing))}")
        bounds = {'zh': (r'最多|至多|不超过', r'最少|至少|不少于'),
                  'en': (r'\b(?:at most|up to|no more than|a maximum of)\b', r'\b(?:at least|no fewer than|no less than|a minimum of)\b'),
                  'ja': (r'最大|上限|[0-9０-９一二三四五六七八九十]+\s*(?:回|個|人|件)?以下', r'最低|少なくとも|[0-9０-９一二三四五六七八九十]+\s*(?:回|個|人|件)?以上')}
        if target_language in {'en','ja'}:
            original_bound = tuple(bool(re.search(pattern,source)) for pattern in bounds['zh'])
            translated_bound = tuple(bool(re.search(pattern,translated,re.I)) for pattern in bounds[target_language])
            if sum(original_bound)==1 and translated_bound==original_bound[::-1] and original_bound[0]!=translated_bound[0]:
                raise TranslationValidationError(row['id'],'译文颠倒了原文的数量上限和下限')
        if len(re.findall(r'[?？؟]',translated))<len(re.findall(r'[?？؟]',source)):
            raise TranslationValidationError(row['id'],'遗漏原文问句，所有问句必须完整翻译并保留问号')
        if uncertain:
            values=sorted(uncertain)
            checks[row['id']]=[dict(code='source_numeric_uncertain',numbers=values,message=f"原文中的数字 {', '.join(values)} 紧邻称呼或人名，可能含识别噪声；请对照原片核对。")]
    return checks
