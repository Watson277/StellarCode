from __future__ import annotations

import re

from bs4 import BeautifulSoup, Tag
from markdownify import markdownify


_NOISE_PATTERN = re.compile(
    r"(?:^|[-_\s])(ad|ads|advert|banner|cookie|comment|footer|header|menu|nav|"
    r"popup|promo|related|share|sidebar|social)(?:$|[-_\s])",
    re.IGNORECASE,
)


class HtmlExtractor:
    def extract(self, html: str) -> str:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(
            [
                "script",
                "style",
                "noscript",
                "nav",
                "aside",
                "footer",
                "header",
                "form",
                "iframe",
                "svg",
            ]
        ):
            tag.decompose()

        for tag in list(soup.find_all(True)):
            if tag.parent is None or tag.attrs is None:
                continue
            marker = " ".join(
                [
                    str(tag.get("id") or ""),
                    " ".join(str(item) for item in (tag.get("class") or [])),
                ]
            )
            if marker and _NOISE_PATTERN.search(marker):
                tag.decompose()

        main = (
            soup.find("article")
            or soup.find("main")
            or soup.find(attrs={"role": "main"})
            or self._wikimedia_body(soup)
            or self._best_content_block(soup)
            or soup.body
        )
        if main is None or len(main.get_text(" ", strip=True)) < 40:
            return ""

        rendered = markdownify(
            str(main),
            heading_style="ATX",
            bullets="-",
            strip=["img"],
        )
        rendered = re.sub(r"[ \t]+\n", "\n", rendered)
        rendered = re.sub(r"\n{3,}", "\n\n", rendered)
        return rendered.strip()

    @staticmethod
    def _wikimedia_body(soup: BeautifulSoup) -> Tag | None:
        lead = soup.find("section", attrs={"data-mw-section-id": "0"})
        return soup.body if lead is not None else None

    @staticmethod
    def _best_content_block(soup: BeautifulSoup) -> Tag | None:
        best: Tag | None = None
        best_score = 0.0
        for candidate in soup.find_all(["section", "div"]):
            text = candidate.get_text(" ", strip=True)
            if len(text) < 120:
                continue
            links = sum(len(link.get_text(" ", strip=True)) for link in candidate.find_all("a"))
            paragraphs = len(candidate.find_all("p"))
            link_density = links / max(len(text), 1)
            score = len(text) * (1.0 - min(link_density, 0.9)) + paragraphs * 80
            if score > best_score:
                best = candidate
                best_score = score
        return best
