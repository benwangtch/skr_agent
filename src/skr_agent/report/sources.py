"""External data the report agent reasons over: the BOM and a news feed.

Both are fixture-backed here. In production ``NewsFeed`` becomes whatever
incident source you subscribe to (or the SDK's built-in ``WebSearch``); the
interface exists so the agent's tool surface does not change when it does.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

__all__ = ["Company", "Article", "BomSource", "NewsFeed", "FixtureBom", "FixtureNewsFeed"]


@dataclass(slots=True)
class Company:
    company_id: str
    name: str
    tier: str
    """Supply-chain criticality, e.g. ``"critical"`` / ``"standard"``."""
    components: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Article:
    url: str
    title: str
    published: str
    source: str
    summary: str
    companies: list[str] = field(default_factory=list)


class BomSource(Protocol):
    def list_companies(self, tier: str | None = None) -> list[Company]: ...
    def get_company(self, company_id: str) -> Company | None: ...


class NewsFeed(Protocol):
    def search(self, query: str, limit: int = 5) -> list[Article]: ...
    def fetch(self, url: str) -> Article | None: ...


@dataclass
class FixtureBom:
    companies: list[Company]

    @classmethod
    def from_fixtures(cls, root: str | Path) -> "FixtureBom":
        raw = json.loads((Path(root) / "bom.json").read_text())
        return cls([Company(**c) for c in raw])

    def list_companies(self, tier: str | None = None) -> list[Company]:
        if tier is None:
            return list(self.companies)
        return [c for c in self.companies if c.tier == tier]

    def get_company(self, company_id: str) -> Company | None:
        for c in self.companies:
            if c.company_id == company_id:
                return c
        return None


@dataclass
class FixtureNewsFeed:
    articles: list[Article]

    @classmethod
    def from_fixtures(cls, root: str | Path) -> "FixtureNewsFeed":
        raw = json.loads((Path(root) / "news.json").read_text())
        return cls([Article(**a) for a in raw])

    def search(self, query: str, limit: int = 5) -> list[Article]:
        terms = [t for t in re.findall(r"\w+", query.lower()) if len(t) > 2]
        if not terms:
            return []
        scored: list[tuple[int, Article]] = []
        for article in self.articles:
            hay = f"{article.title} {article.summary} {' '.join(article.companies)}".lower()
            score = sum(hay.count(t) for t in terms)
            if score:
                scored.append((score, article))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [a for _, a in scored[:limit]]

    def fetch(self, url: str) -> Article | None:
        for a in self.articles:
            if a.url == url:
                return a
        return None
