"""Structure-aware PDF/HTML/Markdown parsing with source locators."""
from dataclasses import dataclass, asdict
from pathlib import Path
import re
from collections import Counter
import pdfplumber
from pypdf import PdfReader
from bs4 import BeautifulSoup


@dataclass
class Block:
    text: str
    heading: str = ''
    page: int | None = None
    kind: str = 'paragraph'
    anchor: str | None = None
    line_start: int | None = None
    line_end: int | None = None


def clean(text):
    return re.sub(r'[ \t]+', ' ', text).strip()


def join_pdf_lines(lines):
    result=''
    for value in lines:
        # Preserve English word boundaries while joining wrapped Chinese text.
        separator='' if not result or re.search(r'[\u3400-\u9fff]$',result) and re.match(r'[\u3400-\u9fff]',value) else ' '
        result+=separator+value
    return result


def parse_text(path):
    """Line-addressable Markdown/TXT, including adjacent headings and fences."""
    value=path.read_bytes().decode('utf-8-sig').replace('\r\n','\n').replace('\r','\n')
    lines=value.splitlines();markdown=path.suffix.lower() in {'.md','.markdown'}
    blocks=[];headings=[];buffer=[];start=1;fence=None;kind='paragraph'
    def flush(end):
        if buffer:
            blocks.append(Block('\n'.join(buffer),' / '.join(headings),kind=kind,line_start=start,line_end=end))
            buffer.clear()
    i=0
    while i<len(lines):
        line=lines[i];number=i+1
        marker=re.match(r'^\s{0,3}(`{3,}|~{3,})(.*)$',line) if markdown else None
        if marker:
            if fence is None:
                flush(number-1);fence=marker.group(1);kind='code';start=number;buffer.append(line)
            elif marker.group(1)[0]==fence[0] and len(marker.group(1))>=len(fence):
                buffer.append(line);flush(number);fence=None;kind='paragraph'
            else:buffer.append(line)
            i+=1;continue
        if fence is not None:buffer.append(line);i+=1;continue
        atx=re.match(r'^ {0,3}(#{1,6})\s+(.+?)(?:\s+#+\s*)?$',line) if markdown else None
        setext=markdown and line.strip() and i+1<len(lines) and re.fullmatch(r' {0,3}(?:=+|-+)\s*',lines[i+1])
        if atx or setext:
            flush(number-1);level=len(atx.group(1)) if atx else 1 if '=' in lines[i+1] else 2
            title=atx.group(2).strip() if atx else line.strip()
            headings=headings[:level-1]+[title]
            end=number if atx else number+1
            blocks.append(Block(title,' / '.join(headings),kind='heading',line_start=number,line_end=end))
            i+=1 if atx else 2;kind='paragraph';continue
        if not line.strip():flush(number-1);kind='paragraph';i+=1;continue
        next_kind='table' if markdown and '|' in line else 'list' if markdown and re.match(r'^\s*(?:[-*+] |\d+[.)] )',line) else 'paragraph'
        if buffer and kind!=next_kind:flush(number-1)
        if not buffer:start=number;kind=next_kind
        buffer.append(line);i+=1
    flush(len(lines))
    return blocks


def parse(path: Path):
    if not path.is_file() or path.stat().st_size > 25 * 1024 * 1024:
        raise ValueError('Document missing or exceeds 25 MiB')
    suffix = path.suffix.lower()
    if suffix == '.pdf':
        reader = PdfReader(path)
        if reader.is_encrypted or len(reader.pages) > 200:
            raise ValueError('Encrypted PDF or page limit exceeded')
        blocks = []
        with pdfplumber.open(path) as pdf:
            for number, page in enumerate(pdf.pages, 1):
                sizes_all=Counter(round(c.get('size',0),1) for c in page.chars if c.get('text','').strip())
                body_size=sizes_all.most_common(1)[0][0] if sizes_all else 10
                tables = page.find_tables()
                def outside(obj):
                    if obj.get('object_type') != 'char':
                        return True
                    x, y = (obj['x0']+obj['x1'])/2, (obj['top']+obj['bottom'])/2
                    return not any(t.bbox[0] <= x <= t.bbox[2] and t.bbox[1] <= y <= t.bbox[3] for t in tables)
                lines = page.filter(outside).extract_text_lines(return_chars=True)
                heading, paragraph, heading_positions = '', [], []
                previous_bottom = None
                def flush():
                    if paragraph:
                        blocks.append(Block(clean(join_pdf_lines(paragraph)), heading, number))
                        paragraph.clear()
                for line in lines:
                    value = clean(line['text'])
                    if not value or re.match(r'^(第\s*\d+\s*页|Page\s+\d+)', value):
                        continue
                    sizes = [c.get('size', 0) for c in line.get('chars', [])]
                    article = re.match(r'第[一二三四五六七八九十百零\d]+条',value)
                    is_heading = bool(sizes and max(sizes) >= max(12,body_size+1.5) and len(value) <= 90)
                    if article:
                        flush();heading=article.group();heading_positions.append((line['top'],heading));paragraph.append(value)
                        previous_bottom=line['bottom'];continue
                    if is_heading:
                        flush()
                        heading = value
                        heading_positions.append((line['top'], value))
                        blocks.append(Block(value,heading,number,'heading'))
                    else:
                        if previous_bottom is not None and line['top']-previous_bottom > 7:
                            flush()
                        paragraph.append(value)
                    previous_bottom = line['bottom']
                flush()
                for table in tables:
                    rows = [[clean(str(c or '').replace('\n', ' ')) for c in row] for row in table.extract()]
                    if len(rows) > 1:
                        table_heading = next((value for top,value in reversed(heading_positions) if top <= table.bbox[1]), heading)
                        blocks.append(Block('\n'.join(' | '.join(row) for row in rows), table_heading, number, 'table'))
                if not any(b.page == number for b in blocks):
                    blocks.append(Block('',page=number,kind='image_without_text' if page.images else 'blank_page'))
        if not any(b.text.strip() for b in blocks):
            raise ValueError('PDF has no extractable text; OCR is required')
        return blocks
    if suffix in {'.html', '.htm'}:
        # html.parser recovers concatenated government header templates containing
        # an early </html>; lxml silently discards the following article in these files.
        soup = BeautifulSoup(path.read_bytes(), 'html.parser')
        for element in soup.select('script,style,nav,footer,form,template,[hidden],[aria-hidden="true"],[role="navigation"],.wy-nav-side,.rst-versions,.wy-breadcrumbs'):
            element.decompose()
        blocks, headings = [], []
        # Explicit article containers outrank a generic role=main in a site header.
        body = next((found for selector in ['#UCAP-CONTENT','#zoom','.TRS_Editor','main','article','[role="main"]']
                     if (found := soup.select_one(selector)) is not None), soup.body or soup)
        article_heading = ''
        for node in body.find_all(['h1','h2','h3','h4','p','li','table']):
            container = node.find_parent(['table','li'])
            if container is not None and (container is body or body in container.parents):
                continue
            if re.match('h[1-4]', node.name):
                level = int(node.name[1])
                headings = headings[:level-1] + [clean(node.get_text(' ', strip=True))]
                article_heading = ''
                continue
            heading = ' / '.join(headings)
            preceding = node.find_previous(lambda tag: re.fullmatch(r'h[1-4]', tag.name or '') and tag.get('id'))
            anchor = node.get('id') or (preceding.get('id') if preceding else None)
            if node.name == 'table':
                rows = [[clean(cell.get_text(' ', strip=True)) for cell in row.find_all(['th','td'])] for row in node.find_all('tr')]
                text = '\n'.join(' | '.join(row) for row in rows if row)
                kind = 'table'
            else:
                text, kind = clean(node.get_text(' ', strip=True)), 'list' if node.name == 'li' else 'paragraph'
                article=re.match(r'第[一二三四五六七八九十百零\d]+条',text)
                if article:article_heading=article.group()
                if article_heading:heading=' / '.join([*headings,article_heading])
            if text:
                blocks.append(Block(text, heading, kind=kind, anchor=anchor))
        return blocks
    if suffix in {'.md','.markdown','.txt'}:return parse_text(path)
    raise ValueError('Supported files: PDF, HTML, Markdown, TXT')
