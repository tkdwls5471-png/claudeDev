"""신뢰할 수 없는 메일 HTML 정제 (프론트는 스크립트가 꺼진 샌드박스 iframe + CSP로 한 번 더 막는다)."""
import re

from bs4 import BeautifulSoup

_REMOTE_URL = re.compile(r'url\(\s*[\'"]?\s*(https?:)?//[^)]*\)', re.I)
_DANGEROUS_TAGS = ['script', 'iframe', 'object', 'embed', 'form', 'base', 'link']
_REMOTE_PREFIXES = ('http://', 'https://', '//')


def sanitize_email_html(html: str, allow_remote_images: bool) -> str:
    """- 실행 가능 요소(script/iframe/object/embed/form/base/link/meta refresh) 제거
    - on* 이벤트 핸들러, javascript: 링크 제거
    - 원격 이미지(http/https, //host, CSS url(), background 속성)는 기본 차단(추적 방지)
    - cid: 참조 이미지는 매핑하지 않으므로 빈 표시로 대체"""
    soup = BeautifulSoup(html or '', 'html.parser')

    for tag in soup(_DANGEROUS_TAGS):
        tag.decompose()
    for tag in soup.find_all('meta'):
        if (tag.get('http-equiv') or '').lower() == 'refresh':
            tag.decompose()

    for tag in soup.find_all(True):
        for attr in list(tag.attrs):
            if attr.lower().startswith('on'):
                del tag.attrs[attr]
        href = tag.attrs.get('href', '')
        if isinstance(href, str) and href.strip().lower().startswith(('javascript:', 'vbscript:', 'data:')):
            tag.attrs['href'] = '#'
        if tag.name == 'a' and tag.attrs.get('href'):
            tag.attrs['target'] = '_blank'
            tag.attrs['rel'] = 'noopener noreferrer'
        if not allow_remote_images:
            style = tag.attrs.get('style')
            if isinstance(style, str) and 'url(' in style.lower():
                tag.attrs['style'] = _REMOTE_URL.sub('none', style)
            bg = tag.attrs.get('background')
            if isinstance(bg, str) and bg.strip().lower().startswith(('http:', 'https:', '//')):
                del tag.attrs['background']
    if not allow_remote_images:
        for st in soup.find_all('style'):
            if st.string:
                st.string.replace_with(_REMOTE_URL.sub('none', st.string))

    for img in soup.find_all('img'):
        img.attrs.pop('srcset', None)  # src만 검사하면 srcset으로 원격 이미지 차단이 우회됨
        src = (img.attrs.get('src') or '').strip()
        if src.lower().startswith('cid:'):
            img.attrs['src'] = ''
            img.attrs['alt'] = img.attrs.get('alt') or '[인라인 이미지: 첨부파일 탭에서 확인]'
        elif src.lower().startswith(_REMOTE_PREFIXES) and not allow_remote_images:
            img.attrs['data-blocked-src'] = src
            img.attrs['src'] = ''
            img.attrs['alt'] = '[원격 이미지 차단됨]'

    return str(soup)
