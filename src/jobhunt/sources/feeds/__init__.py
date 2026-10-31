"""Aggregators, RSS feeds, curated lists and search APIs (phase 3)."""

from jobhunt.sources.feeds.adzuna import AdzunaSource
from jobhunt.sources.feeds.github_lists import SimplifyInternshipsSource, SimplifyNewGradSource
from jobhunt.sources.feeds.hackernews import HackerNewsHiringSource
from jobhunt.sources.feeds.amazon_jobs import AmazonJobsSource
from jobhunt.sources.feeds.enterprise_boards import EightfoldSource, OracleSource, PhenomSource
from jobhunt.sources.feeds.workday import WorkdaySource
from jobhunt.sources.feeds.remote_boards import (
    ArbeitnowSource, HimalayasSource, JobicySource, RemoteOkSource, RemotiveSource, WeWorkRemotelySource,
)

FEED_SOURCE_CLASSES = (
    RemotiveSource,
    RemoteOkSource,
    WeWorkRemotelySource,
    HimalayasSource,
    JobicySource,
    ArbeitnowSource,
    AdzunaSource,
    HackerNewsHiringSource,
    SimplifyInternshipsSource,
    SimplifyNewGradSource,
    WorkdaySource,
    AmazonJobsSource,
    EightfoldSource,
    PhenomSource,
    OracleSource,
)

__all__ = ["FEED_SOURCE_CLASSES"]
