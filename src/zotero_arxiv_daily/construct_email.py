from .protocol import Paper
import math


framework = """
<!DOCTYPE HTML>
<html>
<head>
  <style>
    .star-wrapper {
      font-size: 1.3em; /* 调整星星大小 */
      line-height: 1; /* 确保垂直对齐 */
      display: inline-flex;
      align-items: center; /* 保持对齐 */
    }
    .half-star {
      display: inline-block;
      width: 0.5em; /* 半颗星的宽度 */
      overflow: hidden;
      white-space: nowrap;
      vertical-align: middle;
    }
    .full-star {
      vertical-align: middle;
    }
  </style>
</head>
<body>

<div>
    __CONTENT__
</div>

<br><br>
<div>
To unsubscribe, remove your email in your Github Action setting.
</div>

</body>
</html>
"""

def get_empty_html():
  block_template = """
  <table border="0" cellpadding="0" cellspacing="0" width="100%" style="font-family: Arial, sans-serif; border: 1px solid #ddd; border-radius: 8px; padding: 16px; background-color: #f9f9f9;">
  <tr>
    <td style="font-size: 20px; font-weight: bold; color: #333;">
        No Papers Today. Take a Rest!
    </td>
  </tr>
  </table>
  """
  return block_template

_SOURCE_BADGE_STYLES = {
    "pubmed":   "background-color:#0066cc; color:#fff;",
    "arxiv":    "background-color:#b31b1b; color:#fff;",
    "biorxiv":  "background-color:#9b4dca; color:#fff;",
    "medrxiv":  "background-color:#2a9d8f; color:#fff;",
}
_SOURCE_LABELS = {
    "pubmed":  "PubMed",
    "arxiv":   "arXiv",
    "biorxiv": "bioRxiv",
    "medrxiv": "medRxiv",
}


def _build_source_badge(source: str | None, journal: str | None) -> str:
    """Return an HTML snippet with a colored source badge and optional journal name."""
    if not source:
        return ""
    style = _SOURCE_BADGE_STYLES.get(source.lower(), "background-color:#555; color:#fff;")
    label = _SOURCE_LABELS.get(source.lower(), source.upper())
    badge = (
        f'<span style="{style} border-radius:3px; padding:2px 7px; '
        f'font-size:12px; font-weight:bold;">{label}</span>'
    )
    if journal:
        badge += f'&nbsp;<span style="color:#555; font-style:italic; font-size:12px;">{journal}</span>'
    return badge


def get_block_html(title:str, authors:str, rate:str, tldr:str, pdf_url:str,
                   affiliations:str=None, source:str=None, journal:str=None, stars:str=""):
    source_badge = _build_source_badge(source, journal)
    source_row = (
        f'<tr><td style="padding: 4px 0 6px 0;">{source_badge}</td></tr>'
        if source_badge else ""
    )
    block_template = """
    <table border="0" cellpadding="0" cellspacing="0" width="100%" style="font-family: Arial, sans-serif; border: 1px solid #ddd; border-radius: 8px; padding: 16px; background-color: #f9f9f9;">
    <tr>
        <td style="font-size: 20px; font-weight: bold; color: #333;">
            {title}
        </td>
    </tr>
    {source_row}
    <tr>
        <td style="font-size: 14px; color: #666; padding: 4px 0;">
            {authors}
            <br>
            <i>{affiliations}</i>
        </td>
    </tr>
    <tr>
        <td style="font-size: 14px; color: #333; padding: 8px 0;">
            <strong>Relevance:</strong> {rate} {stars}
        </td>
    </tr>
    <tr>
        <td style="font-size: 14px; color: #333; padding: 8px 0;">
            <strong>TLDR:</strong> {tldr}
        </td>
    </tr>
    <tr>
        <td style="padding: 8px 0;">
            <a href="{pdf_url}" style="display: inline-block; text-decoration: none; font-size: 14px; font-weight: bold; color: #fff; background-color: #d9534f; padding: 8px 16px; border-radius: 4px;">PDF</a>
        </td>
    </tr>
</table>
"""
    return block_template.format(
        title=title, authors=authors, rate=rate, tldr=tldr,
        pdf_url=pdf_url, affiliations=affiliations,
        source_row=source_row, stars=stars,
    )

def get_stars(score:float):
    full_star = '<span class="full-star">⭐</span>'
    half_star = '<span class="half-star">⭐</span>'
    low = 6
    high = 8
    if score <= low:
        return ''
    elif score >= high:
        return full_star * 5
    else:
        interval = (high-low) / 10
        star_num = math.ceil((score-low) / interval)
        full_star_num = int(star_num/2)
        half_star_num = star_num - full_star_num * 2
        return '<div class="star-wrapper">'+full_star * full_star_num + half_star * half_star_num + '</div>'


def get_summary_html(summary: str) -> str:
    return """
    <table border="0" cellpadding="0" cellspacing="0" width="100%" style="font-family: Arial, sans-serif; border: 2px solid #4a90d9; border-radius: 10px; padding: 20px; background-color: #eef5fb; margin-bottom: 16px;">
    <tr>
        <td style="font-size: 18px; font-weight: bold; color: #2c5f8a; padding-bottom: 8px;">
            📋 本期文献速览
        </td>
    </tr>
    <tr>
        <td style="font-size: 14px; color: #333; line-height: 1.7;">
            {summary}
        </td>
    </tr>
    </table>
    """.format(summary=summary.replace("\n", "<br>"))


def render_email(papers: list[Paper], summary: str | None = None) -> str:
    parts = []
    if len(papers) == 0:
        return framework.replace('__CONTENT__', get_empty_html())

    if summary:
        parts.append(get_summary_html(summary))

    for p in papers:
        rate = round(p.score, 1) if p.score is not None else 'Unknown'
        stars = get_stars(p.score) if p.score is not None else ''
        author_list = [a for a in p.authors]
        num_authors = len(author_list)
        if num_authors <= 5:
            authors = ', '.join(author_list)
        else:
            authors = ', '.join(author_list[:3] + ['...'] + author_list[-2:])
        if p.affiliations is not None:
            affiliations = p.affiliations[:5]
            affiliations = ', '.join(affiliations)
            if len(p.affiliations) > 5:
                affiliations += ', ...'
        else:
            affiliations = 'Unknown Affiliation'
        parts.append(get_block_html(
            p.title, authors, rate, p.tldr, p.pdf_url or p.url,
            affiliations=affiliations,
            source=p.source,
            journal=p.journal,
            stars=stars,
        ))

    content = '<br>' + '</br><br>'.join(parts) + '</br>'
    return framework.replace('__CONTENT__', content)
