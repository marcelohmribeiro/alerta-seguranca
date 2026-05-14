import pwd
from fastapi import FastAPI, Query, Body
from typing import Optional
import pandas as pd

# uvicorn src.api.scraper_api:app --reload
# http://127.0.0.1:8000/docs#/default/

# importa funções dos scrapers
from src.ingestion.twitter_web import (
    scrape_twitter_profile,
    scrape_twitter_hashtag,
    scrape_twitter_post,
    scrape_twitter_many
)

from src.ingestion.instagram_web import (
    scrape_instagram_one,
    scrape_instagram_much
)

from src.ingestion.youtube import get_youtube_comments
from src.ingestion.reddit import get_reddit_comments

from src.storage.csv_exporter import save_csv, export_comments_batch

app = FastAPI(title="Scraper API", version="1.0.0")

# Helper: converte DataFrame para JSON
def df_to_json(df: pd.DataFrame):
    return df.to_dict(orient="records")

# ---------------------- TWITTER ----------------------

@app.get("/twitter/web/one")
def twitter_web(mode: str, query: str, limit: int = 10, save: bool = False):
    if mode == "profile":
        data = scrape_twitter_profile(query, limit)
        identifier = f"profile_{query}"
    elif mode == "hashtag":
        data = scrape_twitter_hashtag(query, limit)
        identifier = f"hashtag_{query}"
    elif mode == "post":
        data = scrape_twitter_post(query, limit)
        identifier = f"post_{query}"
    else:
        return {"error": "Modo inválido. Use: profile, hashtag ou post"}
    df = pd.DataFrame(data)
    if save:
        save_csv(df=df, platform="twitter", identifier=identifier, kind=mode)
    return df_to_json(df)

@app.post("/twitter/web/much")
def twitter_web_much(body: dict = Body(...), limit: int = 10, save: bool = False):
    raw = scrape_twitter_many(body, limit)
    export_comments_batch(items=raw.get("profiles", []), platform="twitter", kind="profile", save=save)
    export_comments_batch(items=raw.get("hashtags", []), platform="twitter", kind="hashtag", save=save)
    export_comments_batch(items=raw.get("posts", []), platform="twitter", kind="post", save=save)
    return raw

# ---------------------- INSTAGRAM --------------------

@app.get("/instagram/web/one")
def instagram_web_one(user: str, password: str, mode: str, id: str, limit: int = 10, save: bool = False):
    data = scrape_instagram_one(user, password, mode, id, limit)
    df = pd.DataFrame(data)
    if save:
        csv_path = save_csv(df, platform="instagram", identifier=id, kind=mode)
    response = df_to_json(df)
    return {"data": response, "csv_path": csv_path if save else None}

@app.post("/instagram/web/much")
def instagram_web_much(user: str, password: str, body: dict = Body(...), limit: int = 10, save: bool = False):
    raw = scrape_instagram_much(user, password, body, limit)
    export_comments_batch(items=raw.get("reels", []), platform="instagram", kind="reel", save=save)
    export_comments_batch(items=raw.get("posts", []), platform="instagram", kind="post", save=save)
    return raw

# ---------------------- YOUTUBE ----------------------

@app.get("/scraper/youtube")
def youtube_comments(url: str, limit: int = 20, save: bool = False):
    df = get_youtube_comments(url, limit=limit)
    if save:
        save_csv(df, platform="youtube", identifier="video_comments", kind="videos")
    return df_to_json(df)

# ---------------------- REDDIT ----------------------

@app.get("/scraper/reddit")
def reddit_comments(url: str, limit: int = 20, save: bool = False):
    df = get_reddit_comments(url, limit=limit)
    if save:
        save_csv(df, platform="reddit", identifier=url, kind="posts")
    return df_to_json(df)
