"""
World Cup 2026 Ticket Price Tracker — FastAPI Backend
=====================================================
Endpoints:
  POST /api/auth/signup         — create account
  POST /api/auth/login          — get JWT token
  GET  /api/matches             — browse all matches (filterable)
  GET  /api/matches/{id}        — single match detail
  POST /api/watchlist           — add match + target price
  GET  /api/watchlist           — get your watchlist
  DELETE /api/watchlist/{id}    — remove from watchlist
  GET  /api/prices/{match_id}   — cheapest current price for a match
  POST /api/check-prices        — manual price check against watchlist
"""

import os
import random
from datetime import datetime, timezone, timedelta
from typing import Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr
from supabase import Client, create_client

load_dotenv()

# ─────────────────────────────────────────────────────────────────────────────
# SUPABASE CLIENTS
# We use two clients:
#   supabase       → uses the ANON key  (for auth calls only)
#   supabase_admin → uses SERVICE ROLE key (bypasses RLS for DB operations)
# Never expose the service role key to the browser/frontend.
# ─────────────────────────────────────────────────────────────────────────────
SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
SUPABASE_ANON_KEY: str = os.getenv("SUPABASE_ANON_KEY", "")
SUPABASE_SERVICE_ROLE_KEY: str = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

if not all([SUPABASE_URL, SUPABASE_ANON_KEY, SUPABASE_SERVICE_ROLE_KEY]):
    raise RuntimeError(
        "Missing Supabase env vars. Copy .env.example → .env and fill in your keys."
    )

supabase: Client = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)
supabase_admin: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


# ─────────────────────────────────────────────────────────────────────────────
# APP SETUP
# ─────────────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="WC2026 Ticket Tracker API",
    version="1.0.0",
    description="Track World Cup 2026 ticket prices and get alerts when prices drop.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # ← tighten this to your frontend domain in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─────────────────────────────────────────────────────────────────────────────
# REQUEST / RESPONSE MODELS
# ─────────────────────────────────────────────────────────────────────────────
class SignupRequest(BaseModel):
    email: EmailStr
    password: str


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class WatchlistRequest(BaseModel):
    match_id: str        # UUID of the match from the matches table
    target_price: float  # Alert me when tickets drop to this price (USD)


# ─────────────────────────────────────────────────────────────────────────────
# AUTH DEPENDENCY
# Every protected endpoint uses `user = Depends(get_current_user)`.
# The frontend must send:  Authorization: Bearer <access_token>
# ─────────────────────────────────────────────────────────────────────────────
def get_current_user(authorization: str = Header(...)):
    """
    Verify the Supabase JWT in the Authorization header.
    Returns the Supabase User object on success.
    Raises HTTP 401 on failure.
    """
    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Invalid Authorization header format. Expected: Bearer <token>",
        )

    token = authorization.removeprefix("Bearer ").strip()

    try:
        response = supabase.auth.get_user(token)
        if not response.user:
            raise HTTPException(status_code=401, detail="Invalid or expired token")
        return response.user
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=401, detail="Could not validate credentials")


# ─────────────────────────────────────────────────────────────────────────────
# HEALTH CHECK
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/", tags=["Health"])
def root():
    """Quick sanity check — confirms the API is running."""
    return {
        "status": "ok",
        "message": "WC2026 Ticket Tracker API is running",
        "docs": "/docs",
    }


# ─────────────────────────────────────────────────────────────────────────────
# AUTH ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────
@app.post("/api/auth/signup", tags=["Auth"])
def signup(body: SignupRequest):
    """
    Create a new user account.

    Supabase sends a confirmation email by default.
    The user must confirm their email before they can log in
    (you can disable this in Supabase Dashboard → Auth → Settings).
    """
    try:
        response = supabase.auth.sign_up(
            {"email": body.email, "password": body.password}
        )
        if not response.user:
            raise HTTPException(
                status_code=400,
                detail="Signup failed. Try a different email or a stronger password (min 6 chars).",
            )
        return {
            "message": "Account created! Check your email to confirm your address.",
            "user_id": response.user.id,
            "email": response.user.email,
        }
    except HTTPException:
        raise
    except Exception as e:
        # Supabase throws if email already exists, password too weak, etc.
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/auth/login", tags=["Auth"])
def login(body: LoginRequest):
    """
    Log in with email + password.

    Returns an access_token (JWT). Send this in every subsequent request:
      Authorization: Bearer <access_token>

    Tokens expire after 1 hour. Re-login or use the refresh_token to get a new one.
    """
    try:
        response = supabase.auth.sign_in_with_password(
            {"email": body.email, "password": body.password}
        )
        if not response.user or not response.session:
            raise HTTPException(status_code=401, detail="Invalid email or password")

        return {
            "access_token": response.session.access_token,
            "refresh_token": response.session.refresh_token,
            "token_type": "Bearer",
            "expires_in": response.session.expires_in,
            "user": {
                "id": response.user.id,
                "email": response.user.email,
            },
        }
    except HTTPException:
        raise
    except Exception:
        # Don't leak whether email exists — always return the same message
        raise HTTPException(status_code=401, detail="Invalid email or password")


# ─────────────────────────────────────────────────────────────────────────────
# MATCHES ENDPOINTS  (public — no auth required)
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/api/matches", tags=["Matches"])
def get_matches(
    stage: Optional[str] = None,
    group: Optional[str] = None,
    city: Optional[str] = None,
    team: Optional[str] = None,
):
    """
    Browse all 104 World Cup matches.

    Optional query params:
      ?stage=Group Stage
      ?stage=Final
      ?group=C          (group stage only)
      ?city=Atlanta
      ?team=Brazil      (matches home_team OR away_team)
    """
    try:
        query = supabase_admin.table("matches").select("*").order("match_no")

        if stage:
            query = query.eq("stage", stage)
        if group:
            query = query.eq("group_name", group.upper())
        if city:
            query = query.ilike("city", f"%{city}%")
        if team:
            # PostgREST OR filter: home_team=Brazil,away_team=Brazil
            query = query.or_(f"home_team.ilike.%{team}%,away_team.ilike.%{team}%")

        result = query.execute()
        return {"matches": result.data, "count": len(result.data)}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not fetch matches: {str(e)}")


@app.get("/api/matches/{match_id}", tags=["Matches"])
def get_match(match_id: str):
    """Get full details for a single match by its UUID."""
    try:
        result = (
            supabase_admin.table("matches")
            .select("*")
            .eq("id", match_id)
            .maybe_single()
            .execute()
        )
        if not result.data:
            raise HTTPException(status_code=404, detail="Match not found")
        return result.data
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────────────────────────────────────
# WATCHLIST ENDPOINTS  (auth required)
# ─────────────────────────────────────────────────────────────────────────────
@app.post("/api/watchlist", tags=["Watchlist"])
def add_to_watchlist(body: WatchlistRequest, user=Depends(get_current_user)):
    """
    Add a match to your watchlist with a target price (USD).

    If you already have this match in your watchlist, the target price is updated.
    You'll receive an alert when tickets are found at or below your target price.
    """
    if body.target_price <= 0:
        raise HTTPException(status_code=400, detail="target_price must be greater than 0")

    # Confirm the match exists before adding
    match = (
        supabase_admin.table("matches")
        .select("id, match_no, home_team, away_team, match_date, stadium, city, country")
        .eq("id", body.match_id)
        .maybe_single()
        .execute()
    )
    if not match.data:
        raise HTTPException(status_code=404, detail="Match not found. Check the match_id.")

    try:
        # upsert = insert if new, update target_price if already exists
        result = (
            supabase_admin.table("watchlist")
            .upsert(
                {
                    "user_id": user.id,
                    "match_id": body.match_id,
                    "target_price": body.target_price,
                },
                on_conflict="user_id,match_id",
            )
            .execute()
        )
        entry = result.data[0]
        return {
            "message": "Added to watchlist! We'll alert you when prices drop.",
            "watchlist_id": entry["id"],
            "match": match.data,
            "target_price": entry["target_price"],
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not save watchlist entry: {str(e)}")


@app.get("/api/watchlist", tags=["Watchlist"])
def get_watchlist(user=Depends(get_current_user)):
    """
    Get your full watchlist, including match details and your target price.
    """
    try:
        result = (
            supabase_admin.table("watchlist")
            .select(
                "id, target_price, created_at, "
                "matches(match_no, home_team, away_team, match_date, stadium, city, country, stage, group_name)"
            )
            .eq("user_id", user.id)
            .order("created_at", desc=True)
            .execute()
        )
        return {"watchlist": result.data, "count": len(result.data)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not fetch watchlist: {str(e)}")


@app.delete("/api/watchlist/{watchlist_id}", tags=["Watchlist"])
def remove_from_watchlist(watchlist_id: str, user=Depends(get_current_user)):
    """
    Remove a match from your watchlist.
    You can only delete your own entries.
    """
    try:
        result = (
            supabase_admin.table("watchlist")
            .delete()
            .eq("id", watchlist_id)
            .eq("user_id", user.id)  # security: can only delete own entries
            .execute()
        )
        if not result.data:
            raise HTTPException(
                status_code=404,
                detail="Watchlist entry not found, or it doesn't belong to your account.",
            )
        return {"message": "Removed from watchlist"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────────────────────────────────────
# PRICES ENDPOINT  (public — no auth required)
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/api/prices/{match_id}", tags=["Prices"])
def get_prices(match_id: str):
    """
    Get all ticket prices for a match, sorted cheapest first.

    Only returns prices scraped in the last 24 hours.
    If no prices exist yet, trigger POST /api/check-prices to populate them.
    """
    # Confirm match exists
    match = (
        supabase_admin.table("matches")
        .select("id, match_no, home_team, away_team, match_date, stadium, city, country")
        .eq("id", match_id)
        .maybe_single()
        .execute()
    )
    if not match.data:
        raise HTTPException(status_code=404, detail="Match not found")

    try:
        # Only show prices from the last 24 hours (stale data is misleading)
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()

        result = (
            supabase_admin.table("prices")
            .select("platform, price, url, timestamp")
            .eq("match_id", match_id)
            .gte("timestamp", cutoff)
            .order("price")  # cheapest first
            .execute()
        )

        prices = result.data
        cheapest = prices[0] if prices else None

        return {
            "match": match.data,
            "cheapest": cheapest,
            "all_prices": prices,
            "count": len(prices),
            "note": "Prices from the last 24 hours only. POST /api/check-prices to refresh."
            if not prices
            else None,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not fetch prices: {str(e)}")


# ─────────────────────────────────────────────────────────────────────────────
# CHECK PRICES ENDPOINT  (auth required)
# ─────────────────────────────────────────────────────────────────────────────
@app.post("/api/check-prices", tags=["Prices"])
def check_prices(user=Depends(get_current_user)):
    """
    Manually trigger a price check for every match in your watchlist.

    For each match:
      1. Fetches current ticket prices from platforms (currently stubbed)
      2. Saves them to the prices table
      3. Compares against your target price
      4. Records an alert if your target is met
      5. TODO: sends you an email via Resend

    Returns a summary of what was checked and whether any alerts fired.
    """
    # 1. Load the user's watchlist
    watchlist = (
        supabase_admin.table("watchlist")
        .select("id, target_price, match_id, matches(id, match_no, home_team, away_team, match_date, stadium, city)")
        .eq("user_id", user.id)
        .execute()
    )

    if not watchlist.data:
        return {
            "message": "Your watchlist is empty. Add some matches first via POST /api/watchlist",
            "checked": 0,
            "alerts_triggered": 0,
        }

    results = []
    alerts_triggered = 0

    for entry in watchlist.data:
        match = entry["matches"]
        match_id = entry["match_id"]
        target_price = float(entry["target_price"])

        # 2. Fetch prices (stub — see _scrape_prices() below)
        prices = _scrape_prices(match_id, match)

        # 3. Persist all fetched prices to the database
        if prices:
            try:
                supabase_admin.table("prices").insert(prices).execute()
            except Exception:
                pass  # don't crash the whole check if one insert fails

        # 4. Check if the cheapest price beats the user's target
        below_target = [p for p in prices if p["price"] <= target_price]
        cheapest = min(prices, key=lambda p: p["price"]) if prices else None
        alert_fired = bool(below_target)

        if alert_fired:
            alerts_triggered += 1
            best_deal = below_target[0]

            # Save alert to database
            try:
                supabase_admin.table("alerts").insert(
                    {
                        "user_id": user.id,
                        "match_id": match_id,
                        "price_triggered": best_deal["price"],
                    }
                ).execute()
            except Exception:
                pass  # alert record failure shouldn't block the response

            # ── TODO: Send email via Resend ───────────────────────────────
            # Uncomment and implement when you add Resend:
            #
            # send_price_alert_email(
            #     to_email=user.email,
            #     match=match,
            #     platform=best_deal["platform"],
            #     price=best_deal["price"],
            #     url=best_deal["url"],
            #     target_price=target_price,
            # )
            # ─────────────────────────────────────────────────────────────

        results.append(
            {
                "match": f"#{match['match_no']} {match['home_team']} vs {match['away_team']}",
                "date": match["match_date"],
                "target_price": target_price,
                "cheapest_found": cheapest["price"] if cheapest else None,
                "cheapest_platform": cheapest["platform"] if cheapest else None,
                "cheapest_url": cheapest["url"] if cheapest else None,
                "alert_triggered": alert_fired,
            }
        )

    return {
        "message": "Price check complete",
        "checked": len(results),
        "alerts_triggered": alerts_triggered,
        "results": results,
    }


# ─────────────────────────────────────────────────────────────────────────────
# STUB SCRAPER — Replace this with real scrapers when ready
# ─────────────────────────────────────────────────────────────────────────────
def _scrape_prices(match_id: str, match: dict) -> list[dict]:
    """
    STUB — Returns randomly generated prices for local testing.

    ════════════════════════════════════════════════════════════════
    HOW TO ADD A REAL SCRAPER (example: SeatGeek)
    ════════════════════════════════════════════════════════════════
    1. Sign up for their API: https://platform.seatgeek.com
    2. Set SEATGEEK_CLIENT_ID in your .env
    3. Replace the stub below with something like:

        import httpx
        client_id = os.getenv("SEATGEEK_CLIENT_ID")
        url = f"https://api.seatgeek.com/2/events?q=world+cup&client_id={client_id}"
        r = httpx.get(url, timeout=10)
        data = r.json()
        # parse data["events"] → extract lowest_price, url, etc.

    Platforms to consider:
      • StubHub  — https://developer.stubhub.com  (Fan API, free tier)
      • SeatGeek — https://platform.seatgeek.com  (partner API)
      • Viagogo  — https://developer.viagogo.net   (affiliate API)
    ════════════════════════════════════════════════════════════════
    """
    platforms = [
        ("StubHub",  "https://www.stubhub.com/world-cup-2026-tickets"),
        ("SeatGeek", "https://seatgeek.com/world-cup-2026-tickets"),
        ("Viagogo",  "https://www.viagogo.com/ww/Sports/Soccer/FIFA-World-Cup"),
    ]

    stub_prices = []
    for platform_name, base_url in platforms:
        stub_prices.append(
            {
                "match_id": match_id,
                "platform": platform_name,
                "price": round(random.uniform(150.0, 2000.0), 2),
                "url": f"{base_url}?event={match_id}",
            }
        )

    return stub_prices


# ─────────────────────────────────────────────────────────────────────────────
# PLACEHOLDER — Add Resend email here when ready
# ─────────────────────────────────────────────────────────────────────────────
# def send_price_alert_email(to_email, match, platform, price, url, target_price):
#     """
#     TODO: implement with Resend.
#     pip install resend
#     import resend
#     resend.api_key = os.getenv("RESEND_API_KEY")
#     resend.Emails.send({
#         "from": "alerts@yourdomain.com",
#         "to": to_email,
#         "subject": f"🎟 Price Alert: {match['home_team']} vs {match['away_team']}",
#         "html": f"<p>Tickets found at <b>${price}</b> on {platform}!</p>
#                   <p>Your target was ${target_price}.</p>
#                   <a href='{url}'>Buy now →</a>",
#     })
#     pass
