import unittest

from rockstar_monitor.scraper import parse_articles


class ScraperTests(unittest.TestCase):
    def test_extracts_and_deduplicates_news_links(self):
        html = """
        <html><body>
          <a href="/newswire/article/example-post">Example post</a>
          <a href="/newswire/article/example-post">Duplicate</a>
          <a href="https://example.com/newswire/article/bad">External</a>
          <a href="https://evilrockstargames.com/newswire/article/bad">Lookalike</a>
        </body></html>
        """
        articles = parse_articles(html)
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0].title, "Example post")

    def test_extracts_json_ld(self):
        html = """
        <script type="application/ld+json">
        {"@type":"NewsArticle","headline":"A headline","url":"https://www.rockstargames.com/newswire/article/a-headline","datePublished":"2026-01-01"}
        </script>
        """
        articles = parse_articles(html)
        self.assertEqual(articles[0].published_at, "2026-01-01")


if __name__ == "__main__":
    unittest.main()
