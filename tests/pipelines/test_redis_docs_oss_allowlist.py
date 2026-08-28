"""Coverage for the OSS-only scraper source boundaries."""

from __future__ import annotations

from pathlib import Path

from bs4 import BeautifulSoup

from redis_sre_agent.pipelines.scraper.base import ArtifactStorage, DocumentCategory
from redis_sre_agent.pipelines.scraper.redis_docs import RedisDocsScraper
from redis_sre_agent.pipelines.scraper.redis_docs_local import RedisDocsLocalScraper


def test_web_scraper_has_only_oss_sections(tmp_path: Path) -> None:
    scraper = RedisDocsScraper(ArtifactStorage(tmp_path))

    assert "enterprise_base_url" not in scraper.config
    assert DocumentCategory.OSS.value == "oss"
    assert {category.value for category in DocumentCategory} == {"oss", "shared"}


async def test_web_scraper_filters_non_oss_links(tmp_path: Path) -> None:
    scraper = RedisDocsScraper(ArtifactStorage(tmp_path))
    html = """
        <a href="/docs/commands/get/">GET</a>
        <a href="/docs/operate/rs/references/cli-utilities/">excluded</a>
    """

    links = await scraper._find_documentation_links(
        BeautifulSoup(html, "html.parser"), "https://redis.io/docs/commands/"
    )

    assert links == ["https://redis.io/docs/commands/get"]


def test_local_scraper_excludes_unlisted_paths(tmp_path: Path) -> None:
    scraper = RedisDocsLocalScraper(ArtifactStorage(tmp_path))
    content_dir = tmp_path / "content"
    allowed_file = content_dir / "commands" / "get.md"
    excluded_file = content_dir / "operate" / "rs" / "private.md"
    allowed_file.parent.mkdir(parents=True)
    excluded_file.parent.mkdir(parents=True)
    allowed_file.write_text("# GET\nUseful command documentation.", encoding="utf-8")
    excluded_file.write_text("# Private\nThis must not be ingested.", encoding="utf-8")

    assert scraper._discover_markdown_files(content_dir) == [allowed_file]
