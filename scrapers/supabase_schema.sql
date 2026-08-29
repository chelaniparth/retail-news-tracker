-- Run this once in the Supabase SQL editor for zswfbeziqbtjmckniuxt
-- (Dashboard -> SQL Editor -> New query -> paste -> Run)
--
-- Creates the tables sync_to_supabase.py expects, with a UNIQUE constraint
-- on each table's conflict column so upsert(on_conflict=...) works.
-- Column names are quoted where they use mixed case, to match the CSV
-- headers the scrapers write exactly.

CREATE TABLE IF NOT EXISTS banner_news_master (
    id BIGSERIAL PRIMARY KEY,
    "Store" TEXT,
    "Analyst" TEXT,
    "Industry" TEXT,
    "Type" TEXT,
    "Title" TEXT,
    "Link" TEXT UNIQUE,
    "Published" TEXT,
    "Summary" TEXT,
    "Date_Appended" TEXT
);

CREATE TABLE IF NOT EXISTS businessdebut_master (
    id BIGSERIAL PRIMARY KEY,
    title TEXT,
    link TEXT UNIQUE,
    date TEXT,
    "Date_Appended" TEXT
);

CREATE TABLE IF NOT EXISTS ct_scoop_master (
    id BIGSERIAL PRIMARY KEY,
    heading TEXT,
    date TEXT,
    link TEXT UNIQUE,
    "Date_Appended" TEXT
);

CREATE TABLE IF NOT EXISTS ct_scoop_master_extraction (
    id BIGSERIAL PRIMARY KEY,
    store_name TEXT,
    location TEXT,
    event_type TEXT,
    event_date TEXT,
    status TEXT,
    short_description TEXT,
    article_link TEXT UNIQUE,
    published_date TEXT,
    source_batch TEXT,
    "Date_Appended" TEXT
);

CREATE TABLE IF NOT EXISTS daily_news_master (
    id BIGSERIAL PRIMARY KEY,
    status TEXT,
    industry TEXT,
    region TEXT,
    title TEXT,
    source TEXT,
    published_date TEXT,
    direct_link TEXT UNIQUE,
    keyword TEXT,
    relevance_score TEXT,
    "Date_Appended" TEXT
);

CREATE TABLE IF NOT EXISTS restaurant_master (
    id BIGSERIAL PRIMARY KEY,
    date TEXT,
    title TEXT,
    address TEXT,
    url TEXT UNIQUE,
    "Date_Appended" TEXT
);

CREATE TABLE IF NOT EXISTS restaurant_master_extraction (
    id BIGSERIAL PRIMARY KEY,
    store_name TEXT,
    location TEXT,
    event_type TEXT,
    event_date TEXT,
    status TEXT,
    short_description TEXT,
    article_link TEXT UNIQUE,
    published_date TEXT,
    "Date_Appended" TEXT
);
