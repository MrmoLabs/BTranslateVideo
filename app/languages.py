"""Source and dubbing target languages supported by the installed model family."""
SOURCE_LANGUAGES = {'': '自动检测', 'en': '英语', 'ja': '日语', 'ko': '韩语', 'fr': '法语', 'de': '德语', 'es': '西班牙语', 'pt': '葡萄牙语', 'it': '意大利语', 'ru': '俄语', 'ar': '阿拉伯语', 'hi': '印地语', 'zh': '中文'}

TARGET_LANGUAGES = {code: SOURCE_LANGUAGES[code] for code in ('zh','en','ja','es','ar')}
ISO_LANGUAGES = {'zh':'zho','en':'eng','ja':'jpn','es':'spa','ar':'ara','ko':'kor','fr':'fra','de':'deu','pt':'por','it':'ita','ru':'rus','hi':'hin'}


def target_code(project=None):
    code=project.get('target_language','zh') if isinstance(project,dict) else project or 'zh'
    if code not in TARGET_LANGUAGES:
        raise ValueError('目标语言不受支持，请选择中文、英语、日语、西班牙语或阿拉伯语')
    return code


def target_files(project):
    return dict(audio='chinese.wav',subtitles='chinese.srt',captions='chinese.vtt') if target_code(project)=='zh' else dict(audio='translated.wav',subtitles='translated.srt',captions='translated.vtt')


def tts_language(project, version='2.5'):
    code=target_code(project)
    if version!='2.5' and code not in {'zh','en'}:
        raise ValueError('当前目标语言需要 IndexTTS-2.5，请在设置中切换模型版本')
    return code.upper()
