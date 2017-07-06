# -*- coding: utf-8 -*-
"""
In this module, you can find the Webspider class.
It's a virtual class and shouldn't be used to crawl anything.
"""

import datetime
import hashlib
import os
from urlparse import urlparse

import igraph as ig

from elasticsearch.helpers import scan
from scrapyelasticsearch.scrapyelasticsearch import ElasticSearchPipeline

from scrapy import signals
from scrapy.conf import settings
from scrapy.http import Request
from scrapy.http.response.html import HtmlResponse
from scrapy.loader import ItemLoader
from scrapy.spiders import CrawlSpider, Rule

# For text field
import html2text
from scrapy.selector import HtmlXPathSelector

from ahmia.items import DocumentItem, LinkItem, AuthorityItem

class WebSpider(CrawlSpider):
    """
    The base to crawl webpages in a specific network (tor, i2p).
    It uses github.com/ahmia/ahmia-index mappings.
    """
    name = None

    default_start_url = None

    @classmethod
    def from_crawler(cls, crawler, *args, **kwargs):
        spider = super(WebSpider, cls).from_crawler(crawler, *args, **kwargs)
        if settings.get('FULL_PAGERANK_COMPUTE', False):
            crawler.signals.connect(spider.on_idle, signals.spider_idle)
        return spider

    def __init__(self, *args, **kwargs):
        self.rules = [Rule(self.get_link_extractor(),
                           callback=self.parse_item,
                           process_links=self.limit_links,
                           follow=True)]
        super(WebSpider, self).__init__(*args, **kwargs)
        target_sites = settings.get('TARGET_SITES')
        if target_sites and os.path.isfile(target_sites):
            # Read a list of URLs from file
            # Create the target file list
            with open(target_sites) as target_sites_file:
                # Make it to Python list
                self.start_urls = target_sites_file.read().splitlines()
                # Remove empty strings
                self.start_urls = [u for u in self.start_urls if u]
        else:
            self.start_urls = self.default_start_url

    def get_link_extractor(self):
        """ Returns the LinkExtractor.
        Must be overriden in each Spider. """
        raise NotImplementedError

    def limit_links(self, links):
        """ Reduce the number of links for each page """
        if len(links) > 500:
            links = links[:250] + links[-250:]
        return links

    def build_links(self):
        """ Build a complete list of links from html in elasticsearch """
        def binarySearch(array, key, low, high):
            """ Fast search in a sorted array """
            if low > high: # termination case
                return -1
            middle = (low + high) / 2 # gets the middle of the array
            if array[middle] == key:  # if the middle is our key
                return middle
            elif key < array[middle]: # our key might be in the left sub-array
                return binarySearch(array, key, low, middle-1)
            else:                     # our key might be in the right sub-array
                return binarySearch(array, key, middle+1, high)

        es_obj = ElasticSearchPipeline.from_crawler(self.crawler).es
        new_links = []
        hashes = sorted([
            url['_id']for url in scan(
                es_obj,
                query={
                    "query": {
                        "exists": {
                            "field": "url"
                        }
                    }
                },
                index=self.settings['ELASTICSEARCH_INDEX'],
                doc_type=self.settings['ELASTICSEARCH_TYPE'],
                _source_exclude=["*"])
        ])
        urls_iter = scan(
            es_obj,
            query={
                "query": {
                    "exists": {
                        "field": "content"
                    }
                }
            },
            index=self.settings['ELASTICSEARCH_INDEX'],
            doc_type=self.settings['ELASTICSEARCH_TYPE'],
            _source_include=["content", "url"]
        )

        for hit in urls_iter:
            id_ = hit['_id']
            url = hit['_source']['url']
            content = hit['_source']['content']
            if isinstance(content, str):
                content = unicode(content, "utf-8")
            try:
                response = HtmlResponse(url, encoding="utf-8", body=content)
                for request in self._requests_to_follow(response):
                    hash_target = hashlib.sha1(request.url).hexdigest()
                    if binarySearch(hashes, hash_target, 0, len(hashes)-1) < 0:
                        continue
                    new_links.append((id_,
                                      hash_target))
            except TypeError:
                pass

        return new_links

    def compute_pagerank(self):
        """ Compute the pagerank dict """
        new_links = self.build_links()

        nodes = set([url_hash for link in new_links for url_hash in link])
        links_graph = ig.Graph(len(nodes))
        links_graph.vs["name"] = list(nodes)
        links_graph.add_edges(new_links)

        itemproc = self.crawler.engine.scraper.itemproc
        for i, score in enumerate(links_graph.pagerank()):
            itemproc.process_item(AuthorityItem(url=links_graph.vs["name"][i],
                                                score=score),
                                  self)

    def parse(self, response):
        """ Parse a response. This new version yields every request
        gotten by following links on a page into a LinkItem. """
        for request_or_item in super(WebSpider, self).parse(response):
            if isinstance(request_or_item, Request):
                yield LinkItem(target=request_or_item.url,
                               source=response.url,
                               anchor=request_or_item.meta['link_text'])
            yield request_or_item

    def on_idle(self, spider):
        """ Called when no more requests is in the queue and no more item
        is remaining in the pipeline """
        spider.compute_pagerank()

    def detect_encoding(self, response):
        return response.headers.encoding or "utf-8"

    def html2string(self, response):
        """HTML 2 string converter. Returns a string."""
        converter = html2text.HTML2Text()
        converter.ignore_links = True
        encoding = self.detect_encoding(response)
        decoded_html = response.body.decode(encoding, 'ignore')
        string = converter.handle(decoded_html)
        return string

    def parse_item(self, response):
        """ Parse a response into a DocumentItem. """
        doc_loader = ItemLoader(item=DocumentItem(), response=response)
        doc_loader.add_value('url', response.url)
        doc_loader.add_xpath('meta', '//meta[@name=\'description\']/@content')
        doc_loader.add_value('domain', urlparse(response.url).hostname)
        doc_loader.add_xpath('title', '//title/text()')

        hxs = HtmlXPathSelector(response) # For HTML extractions

        # Extract links
        # For each link on this page
        links = []
        a_links = hxs.xpath('//a')
        for link in a_links:
            link_obj = {}
            # Extract the link's URL
            link_str = " ".join(link.xpath('@href').extract())
            link_obj['link'] = link_str.replace("\n", "")
            # Extract the links value
            link_name_str = " ".join(link.xpath('text()').extract())
            link_name_str = link_name_str.replace("\n", "")
            link_name_str = link_name_str.lstrip()
            link_name_str = link_name_str.rstrip()
            link_obj['link_name'] = link_name_str
            links.append(link_obj)
        doc_loader.add_value('links', links)

        # Populate text field
        title_list = hxs.xpath('//title/text()').extract()
        title = ' '.join(title_list)
        body_text = self.html2string(response)
        text = title + " " + body_text
        doc_loader.add_value('content', text)
        doc_loader.add_value('raw_text', text)

        doc_loader.add_value('raw_title', title)
        doc_loader.add_value('raw_url', response.url)

        h1_list = hxs.xpath("//h1/text()").extract()
        doc_loader.add_value('h1', " ".join(h1_list))

        doc_loader.add_value('content_type', response.headers['Content-type'])
        doc_loader.add_value('updated_on', datetime.datetime.now().strftime(
            "%Y-%m-%dT%H:%M:%S"))
        item = doc_loader.load_item()
        return item
