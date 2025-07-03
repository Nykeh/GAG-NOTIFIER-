import os
import json
import discord
import asyncio
from discord.ext import commands, tasks
from discord import app_commands
from discord.ui import View, Button
import aiohttp
from dotenv import load_dotenv
from datetime import datetime, timezone, timedelta
import time

load_dotenv()
TOKEN = os.getenv("DISCORD_BOT_TOKEN")

CONFIG_FILE = "channels.json"
LAST_STATE_FILE = "last_state.json"

# --- Load and Save Channel IDs ---
def load_channels():
    global seed_channel_id, gear_channel_id, egg_channel_id
    global cosmetic_channel_id, announcement_channel_id, weather_channel_id
    global event_stock_channel_id

    if not os.path.isfile(CONFIG_FILE):
        return
    with open(CONFIG_FILE, "r") as f:
        data = json.load(f)

    seed_channel_id         = data.get("seed_channel_id")
    gear_channel_id         = data.get("gear_channel_id")
    egg_channel_id          = data.get("egg_channel_id")
    cosmetic_channel_id     = data.get("cosmetic_channel_id")
    announcement_channel_id = data.get("announcement_channel_id")
    weather_channel_id      = data.get("weather_channel_id")
    event_stock_channel_id  = data.get("event_stock_channel_id")

def save_channels():
    data = {
        "seed_channel_id":         seed_channel_id,
        "gear_channel_id":         gear_channel_id,
        "egg_channel_id":          egg_channel_id,
        "cosmetic_channel_id":     cosmetic_channel_id,
        "announcement_channel_id": announcement_channel_id,
        "weather_channel_id":      weather_channel_id,
        "event_stock_channel_id":  event_stock_channel_id
    }
    with open(CONFIG_FILE, "w") as f:
        json.dump(data, f, indent=2)

# --- Load and Save Last Sent State ---
def load_last_state():
    global last_state
    if os.path.isfile(LAST_STATE_FILE):
        with open(LAST_STATE_FILE, "r") as f:
            last_state = json.load(f)
    else:
        last_state = {
            "seed": 0,
            "gear": 0,
            "egg": 0,
            "cosmetic": 0,
            "event_stock": 0,
            "announcement": 0,
            "weather": {}
        }
    
    # Convert legacy weather format (list) to new dict format
    if isinstance(last_state.get("weather"), list):
        print("⚠️ Migrating weather state from list to dict")
        last_state["weather"] = {}

def save_last_state():
    with open(LAST_STATE_FILE, "w") as f:
        json.dump(last_state, f, indent=2)

# Bot Setup
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Channel placeholders
seed_channel_id = gear_channel_id = egg_channel_id = None
cosmetic_channel_id = announcement_channel_id = weather_channel_id = None
event_stock_channel_id = None

load_channels()
load_last_state()

# Lock for state access
state_lock = asyncio.Lock()

# Constants
STOCK_API_URL   = "YOUR_API"
WEATHER_API_URL = "YOUR_API"
INVITE_URL      = "bot invite url here"

# Stock category mapping
STOCK_CATEGORY_MAPPING = {
    "seed": ("seed_stock", "Seeds 🌱"),
    "gear": ("gear_stock", "Gear ⚙️"),
    "egg": ("egg_stock", "Eggs 🥚"),
    "cosmetic": ("cosmetic_stock", "Cosmetics 💄"),
    "event_stock": ("eventshop_stock", "Event Stock 🎉")
}

# Helper to get channel ID for stock category
def get_channel_for_category(category_key):
    if category_key == "seed":
        return seed_channel_id
    elif category_key == "gear":
        return gear_channel_id
    elif category_key == "egg":
        return egg_channel_id
    elif category_key == "cosmetic":
        return cosmetic_channel_id
    elif category_key == "event_stock":
        return event_stock_channel_id
    return None

# Immediate check functions for all event types
async def check_new_stock_for_category(category_key: str, api_key: str, channel_id: int, title: str):
    """Check for new stock in a specific category and send if available"""
    print(f"🔍 Checking new stock for {category_key}...")
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(STOCK_API_URL) as r:
                if r.content_type == 'application/json':
                    raw = await r.json()
                    stock = raw[0] if isinstance(raw, list) else raw
                else:
                    text = await r.text()
                    print(f"Stock API returned non-JSON: {text[:200]}")
                    return
        except Exception as e:
            print(f"Stock API Error in immediate check: {e}")
            return

    items = stock.get(api_key, [])
    if not items:
        return

    # Get timestamps
    start_ts = max(i.get("start_date_unix", 0) for i in items)
    end_ts = max(i.get("end_date_unix", 0) for i in items)

    # Check if this is new stock
    async with state_lock:
        if start_ts <= last_state.get(category_key, 0):
            print(f"⏩ Skipping {category_key} - no new stock")
            return  # Not new
        last_state[category_key] = start_ts  # Reserve this timestamp

    # Try to send new stock
    try:
        ch = bot.get_channel(channel_id)
        if ch:
            embed = create_stock_embed(items, title, start_ts, end_ts)
            msg = await ch.send(embed=embed, view=create_invite_view())
            print(f"✅ Sent new {category_key} stock to channel {channel_id}")
            
            # Update active events
            active_events["stock"][category_key] = {
                "message_id": msg.id,
                "channel_id": channel_id,
                "start_ts": start_ts,
                "end_ts": end_ts,
                "items": items,
                "title": title
            }
            save_last_state()  # Persist state
    except Exception as e:
        print(f"Error sending new stock for {category_key}: {e}")

async def process_weather_event(w: dict, is_restart: bool = False):
    """Process a single weather event (new or existing)"""
    try:
        weather_id = w.get("weather_id")
        if not weather_id:
            return False
            
        start_ts = w.get("start_duration_unix", 0)
        active = w.get("active", False)
        
        # Skip if not active
        if not active:
            return False
            
        # Calculate end time
        duration = w.get("duration", 0)
        end_ts = w.get("end_duration_unix")
        if end_ts is None and start_ts and duration:
            end_ts = start_ts + duration
            
        # Skip if event has ended
        if end_ts and end_ts < datetime.now(timezone.utc).timestamp():
            return False
            
        # Check if we've processed this specific occurrence
        async with state_lock:
            # Get stored start time for this weather ID
            stored_start = last_state["weather"].get(weather_id, 0)
            
            # If this is a different occurrence (new start time)
            if start_ts != stored_start:
                # Send weather embed
                embed = create_weather_embed(w)
                ch = bot.get_channel(weather_channel_id)
                if ch:
                    msg = await ch.send(embed=embed, view=create_invite_view())
                    weather_name = w.get("weather_name", "Unknown Weather")
                    print(f"✅ Sent {'RESTART ' if is_restart else ''}weather event: {weather_name} (ID: {weather_id}) to channel {weather_channel_id}")
                    
                    # Track for updates
                    active_events["weather"][weather_id] = {
                        "message_id": msg.id,
                        "channel_id": weather_channel_id,
                        "weather": w
                    }
                    
                    # Update state with new start time
                    last_state["weather"][weather_id] = start_ts
                    return True
        return False
    except Exception as e:
        print(f"⚠️ Error processing weather item: {e}")
        return False

async def check_new_weather(is_restart: bool = False):
    """Check for weather events, with option to handle restart cases"""
    print("\n🌡️ Checking for weather events...")
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(WEATHER_API_URL) as r:
                if r.status == 200 and r.content_type == 'application/json':
                    data = await r.json()
                    # Correctly parse the weather array from the API response
                    wlist = data.get("weather", [])
                    print(f"🌤️ Received {len(wlist)} weather events from API")
                else:
                    text = await r.text()
                    print(f"⚠️ Weather API returned non-JSON: {text[:200]}")
                    return
        except Exception as e:
            print(f"⚠️ Weather API Error: {e}")
            return
    
    new_events_count = 0
    for w in wlist:
        if not isinstance(w, dict):
            continue
            
        if await process_weather_event(w, is_restart):
            new_events_count += 1
    
    if new_events_count > 0:
        save_last_state()
        print(f"🌧️ Processed {new_events_count} weather events")
    else:
        print("🌤️ No new weather events found")

async def check_new_announcements():
    """Immediately check for new Jandel announcements"""
    print("\n📝 Checking for new announcements...")
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(STOCK_API_URL) as r:
                if r.status == 200 and r.content_type == 'application/json':
                    raw = await r.json()
                    stock = raw[0] if isinstance(raw, list) else raw
                    print(f"📢 Received stock API response")
                else:
                    text = await r.text()
                    print(f"⚠️ Stock API returned non-JJSON: {text[:200]}")
                    return
        except Exception as e:
            print(f"⚠️ Stock API Error: {e}")
            return

    raw_note = stock.get("notification", [])
    note = raw_note[0] if isinstance(raw_note, list) and raw_note else None
    if note and isinstance(note, dict):
        msg_content = note.get("message")
        ts = note.get("timestamp", 0)
        
        async with state_lock:
            if not msg_content or ts <= last_state.get("announcement", 0):
                print("⏩ No new announcements found")
                return
                
        # Send new announcement
        embed = discord.Embed(
            title="📝 Jandel Announcement",
            description=msg_content,
            color=discord.Color.orange()
        )
        embed.add_field(name="🕒 Posted", value=f"{time_ago(ts)}", inline=False)
        
        end_ts = note.get("end_timestamp")
        if end_ts:
            now = datetime.now(timezone.utc).timestamp()
            if end_ts > now:
                remaining = end_ts - now
                mins = int(remaining // 60)
                secs = int(remaining % 60)
                embed.add_field(name="⏱️ Ends In", value=f"{mins}m {secs}s", inline=True)
        
        ch = bot.get_channel(announcement_channel_id)
        if ch:
            msg = await ch.send(embed=embed, view=create_invite_view())
            print(f"✅ Sent NEW announcement to channel {announcement_channel_id}")
            
            # Track for updates
            active_events["announcements"][ts] = {
                "message_id": msg.id,
                "channel_id": announcement_channel_id,
                "start_ts": ts,
                "end_ts": end_ts,
                "content": msg_content
            }
            
        # Update state
        async with state_lock:
            last_state["announcement"] = ts
        save_last_state()
    else:
        print("⏩ No announcements found in API response")

# Full Data definitions (fruits, mutations, variants)
DATA = {
    "fruits": [
        {"item_id":"carrot","display_name":"Carrot","baseValue":20,"weightDivisor":0.275},
        {"item_id":"strawberry","display_name":"Strawberry","baseValue":15,"weightDivisor":0.3},
        {"item_id":"blueberry","display_name":"Blueberry","baseValue":20,"weightDivisor":0.2},
        {"item_id":"orange_tulip","display_name":"Orange Tulip","baseValue":850,"weightDivisor":0.05},
        {"item_id":"tomato","display_name":"Tomato","baseValue":30,"weightDivisor":0.5},
        {"item_id":"corn","display_name":"Corn","baseValue":40,"weightDivisor":2},
        {"item_id":"daffodil","display_name":"Daffodil","baseValue":1000,"weightDivisor":0.2},
        {"item_id":"watermelon","display_name":"Watermelon","baseValue":3000,"weightDivisor":7},
        {"item_id":"pumpkin","display_name":"Pumpkin","baseValue":3400,"weightDivisor":8},
        {"item_id":"apple","display_name":"Apple","baseValue":275,"weightDivisor":3},
        {"item_id":"bamboo","display_name":"Bamboo","baseValue":4000,"weightDivisor":4},
        {"item_id":"coconut","display_name":"Coconut","baseValue":400,"weightDivisor":14},
        {"item_id":"cactus","display_name":"Cactus","baseValue":3400,"weightDivisor":7},
        {"item_id":"dragon_fruit","display_name":"Dragon Fruit","baseValue":4750,"weightDivisor":12},
        {"item_id":"mango","display_name":"Mango","baseValue":6500,"weightDivisor":15},
        {"item_id":"grape","display_name":"Grape","baseValue":7850,"weightDivisor":3},
        {"item_id":"mushroom","display_name":"Mushroom","baseValue":151000,"weightDivisor":25},
        {"item_id":"pepper","display_name":"Pepper","baseValue":8000,"weightDivisor":5},
        {"item_id":"cacao","display_name":"Cacao","baseValue":12000,"weightDivisor":8},
        {"item_id":"beanstalk","display_name":"Beanstalk","baseValue":28000,"weightDivisor":10},
        {"item_id":"ember_lily","display_name":"Ember Lily","baseValue":66666,"weightDivisor":12},
        {"item_id":"sugar_apple","display_name":"Sugar Apple","baseValue":48000,"weightDivisor":9},
        {"item_id":"pineapple","display_name":"Pineapple","baseValue":2000,"weightDivisor":3},
        {"item_id":"cauliflower","display_name":"Cauliflower","baseValue":40,"weightDivisor":5},
        {"item_id":"green_apple","display_name":"Green Apple","baseValue":300,"weightDivisor":3},
        {"item_id":"banana","display_name":"Banana","baseValue":2000,"weightDivisor":1.5},
        {"item_id":"avocado","display_name":"Avocado","baseValue":350,"weightDivisor":6.5},
        {"item_id":"kiwi","display_name":"Kiwi","baseValue":2750,"weightDivisor":5},
        {"item_id":"bell_pepper","display_name":"Bell Pepper","baseValue":5500,"weightDivisor":8},
        {"item_id":"prickly_pear","display_name":"Prickly Pear","baseValue":7000,"weightDivisor":7},
        {"item_id":"feijoa","display_name":"Feijoa","baseValue":13000,"weightDivisor":10},
        {"item_id":"loquat","display_name":"Loquat","baseValue":8000,"weightDivisor":6.5},
        {"item_id":"wild_carrot","display_name":"Wild Carrot","baseValue":25000,"weightDivisor":0.3},
        {"item_id":"pear","display_name":"Pear","baseValue":20000,"weightDivisor":3},
        {"item_id":"cantaloupe","display_name":"Cantaloupe","baseValue":34000,"weightDivisor":5.5},
        {"item_id":"parasol_flower","display_name":"Parasol Flower","baseValue":200000,"weightDivisor":6},
        {"item_id":"rosy_delight","display_name":"Rosy Delight","baseValue":69000,"weightDivisor":10},
        {"item_id":"elephant_ears","display_name":"Elephant Ears","baseValue":77000,"weightDivisor":18},
        {"item_id":"chocolate_carrot","display_name":"Chocolate Carrot","baseValue":11000,"weightDivisor":0.275},
        {"item_id":"red_lollipop","display_name":"Red Lollipop","baseValue":50000,"weightDivisor":4},
        {"item_id":"blue_lollipop","display_name":"Blue Lollipop","baseValue":50000,"weightDivisor":1},
        {"item_id":"candy_sunflower","display_name":"Candy Sunflower","baseValue":80000,"weightDivisor":1.5},
        {"item_id":"easter_egg","display_name":"Easter Egg","baseValue":2500,"weightDivisor":3},
        {"item_id":"candy_blossom","display_name":"Candy Blossom","baseValue":100000,"weightDivisor":3},
        {"item_id":"peach","display_name":"Peach","baseValue":300,"weightDivisor":2},
        {"item_id":"raspberry","display_name":"Raspberry","baseValue":100,"weightDivisor":0.75},
        {"item_id":"papaya","display_name":"Papaya","baseValue":1000,"weightDivisor":3},
        {"item_id":"banana","display_name":"Banana","baseValue":1750,"weightDivisor":1.5},
        {"item_id":"passionfruit","display_name":"Passionfruit","baseValue":3550,"weightDivisor":3},
        {"item_id":"soul_fruit","display_name":"Soul Fruit","baseValue":7750,"weightDivisor":25},
        {"item_id":"cursed_fruit","display_name":"Cursed Fruit","baseValue":25750,"weightDivisor":30},
        {"item_id":"mega_mushroom","display_name":"Mega Mushroom","baseValue":500,"weightDivisor":70},
        {"item_id":"cherry_blossom","display_name":"Cherry Blossom","baseValue":500,"weightDivisor":3},
        {"item_id":"purple_cabbage","display_name":"Purple Cabbage","baseValue":500,"weightDivisor":5},
        {"item_id":"lemon","display_name":"Lemon","baseValue":350,"weightDivisor":1},
        {"item_id":"pink_tulip","display_name":"Pink Tulip","baseValue":850,"weightDivisor":0.05},
        {"item_id":"cranberry","display_name":"Cranberry","baseValue":3500,"weightDivisor":1},
        {"item_id":"durian","display_name":"Durian","baseValue":7500,"weightDivisor":8},
        {"item_id":"eggplant","display_name":"Eggplant","baseValue":12000,"weightDivisor":5},
        {"item_id":"lotus","display_name":"Lotus","baseValue":35000,"weightDivisor":20},
        {"item_id":"venus_fly_trap","display_name":"Venus Fly Trap","baseValue":85000,"weightDivisor":10},
        {"item_id":"nightshade","display_name":"Nightshade","baseValue":3500,"weightDivisor":0.5},
        {"item_id":"glowshroom","display_name":"Glowshroom","baseValue":300,"weightDivisor":0.75},
        {"item_id":"mint","display_name":"Mint","baseValue":5250,"weightDivisor":1},
        {"item_id":"moonflower","display_name":"Moonflower","baseValue":9500,"weightDivisor":2},
        {"item_id":"starfruit","display_name":"Starfruit","baseValue":15000,"weightDivisor":3},
        {"item_id":"moonglow","display_name":"Moonglow","baseValue":25000,"weightDivisor":7},
        {"item_id":"moon_blossom","display_name":"Moon Blossom","baseValue":66666,"weightDivisor":3},
        {"item_id":"crimson_vine","display_name":"Crimson Vine","baseValue":1250,"weightDivisor":1},
        {"item_id":"moon_melon","display_name":"Moon Melon","baseValue":18000,"weightDivisor":8},
        {"item_id":"blood_banana","display_name":"Blood Banana","baseValue":6000,"weightDivisor":1.5},
        {"item_id":"celestiberry","display_name":"Celestiberry","baseValue":10000,"weightDivisor":2},
        {"item_id":"moon_mango","display_name":"Moon Mango","baseValue":50000,"weightDivisor":15},
        {"item_id":"rose","display_name":"Rose","baseValue":5000,"weightDivisor":1},
        {"item_id":"foxglove","display_name":"Foxglove","baseValue":20000,"weightDivisor":2},
        {"item_id":"lilac","display_name":"Lilac","baseValue":35000,"weightDivisor":3},
        {"item_id":"pink_lily","display_name":"Pink Lily","baseValue":65000,"weightDivisor":6},
        {"item_id":"purple_dahlia","display_name":"Purple Dahlia","baseValue":75000,"weightDivisor":12},
        {"item_id":"sunflower","display_name":"Sunflower","baseValue":160000,"weightDivisor":16.5},
        {"item_id":"lavender","display_name":"Lavender","baseValue":25000,"weightDivisor":0.275},
        {"item_id":"nectarshade","display_name":"Nectarshade","baseValue":50000,"weightDivisor":0.8},
        {"item_id":"nectarine","display_name":"Nectarine","baseValue":48000,"weightDivisor":3},
        {"item_id":"hive_fruit","display_name":"Hive Fruit","baseValue":62000,"weightDivisor":8},
        {"item_id":"manuka_flower","display_name":"Manuka Flower","baseValue":25000,"weightDivisor":0.3},
        {"item_id":"dandelion","display_name":"Dandelion","baseValue":50000,"weightDivisor":4},
        {"item_id":"lumira","display_name":"Lumira","baseValue":85000,"weightDivisor":6},
        {"item_id":"honeysuckle","display_name":"Honeysuckle","baseValue":100000,"weightDivisor":12},
        {"item_id":"crocus","display_name":"Crocus","baseValue":30000,"weightDivisor":0.275},
        {"item_id":"succulent","display_name":"Succulent","baseValue":25000,"weightDivisor":5},
        {"item_id":"violet_corn","display_name":"Violet Corn","baseValue":50000,"weightDivisor":3},
        {"item_id":"bendboo","display_name":"Bendboo","baseValue":155000,"weightDivisor":18},
        {"item_id":"cocovine","display_name":"Cocovine","baseValue":66666,"weightDivisor":14},
        {"item_id":"dragon_pepper","display_name":"Dragon Pepper","baseValue":88888,"weightDivisor":6},
        {"item_id":"bee_balm","display_name":"Bee Balm","baseValue":18000,"weightDivisor":1},
        {"item_id":"nectar_thorn","display_name":"Nectar Thorn","baseValue":44444,"weightDivisor":7},
        {"item_id":"suncoil","display_name":"Suncoil","baseValue":80000,"weightDivisor":10},
        {"item_id":"noble_flower","display_name":"Noble Flower","baseValue":20000,"weightDivisor":5},
        {"item_id":"traveler's_fruit","display_name":"Traveler's Fruit","baseValue":20000,"weightDivisor":2},
        {"item_id":"ice_cream_bean","display_name":"Ice Cream Bean","baseValue":4500,"weightDivisor":4},
        {"item_id":"lime","display_name":"Lime","baseValue":1000,"weightDivisor":1}
    ],
    "mutations": [
        {"mutation_id":"windstruck","display_name":"Windstruck","multiplier":5},
        {"mutation_id":"twisted","display_name":"Twisted","multiplier":5},
        {"mutation_id":"voidtouched","display_name":"Voidtouched","multiplier":135},
        {"mutation_id":"moonlit","display_name":"Moonlit","multiplier":2},
        {"mutation_id":"pollinated","display_name":"Pollinated","multiplier":3},
        {"mutation_id":"honeyglazed","display_name":"HoneyGlazed","multiplier":5},
        {"mutation_id":"plasma","display_name":"Plasma","multiplier":5},
        {"mutation_id":"molten","display_name":"Molten","multiplier":25},
        {"mutation_id":"frozen","display_name":"Frozen","multiplier":10},
        {"mutation_id":"celestial","display_name":"Celestial","multiplier":120},
        {"mutation_id":"burnt","display_name":"Burnt","multiplier":4},
        {"mutation_id":"dawnbound","display_name":"Dawnbound","multiplier":150},
        {"mutation_id":"shocked","display_name":"Shocked","multiplier":100},
        {"mutation_id":"bloodlit","display_name":"Bloodlit","multiplier":4},
        {"mutation_id":"chilled","display_name":"Chilled","multiplier":2},
        {"mutation_id":"choc","display_name":"Choc","multiplier":2},
        {"mutation_id":"zombified","display_name":"Zombified","multiplier":25},
        {"mutation_id":"heavenly","display_name":"Heavenly","multiplier":5},
        {"mutation_id":"cooked","display_name":"Cooked","multiplier":10},
        {"mutation_id":"disco","display_name":"Disco","multiplier":125},
        {"mutation_id":"wet","display_name":"Wet","multiplier":3},
        {"mutation_id":"sweet","display_name":"Sweet","multiplier":2},
        {"mutation_id":"swampy","display_name":"Swampy","multiplier":1},
        {"mutation_id":"ghostly","display_name":"Ghostly","multiplier":90},
        {"mutation_id":"meteoric","display_name":"Meteoric","multiplier":125}
    ],
    "variants": [
        {"variant_id":"normal","display_name":"Normal","multiplier":1},
        {"variant_id":"gold","display_name":"Gold","multiplier":20},
        {"variant_id":"rainbow","display_name":"Rainbow","multiplier":50}
    ]
}
# Lookup tables
FRUIT_DATA = DATA["fruits"]
MUTATIONS   = {m["mutation_id"]: m["multiplier"] for m in DATA["mutations"]}
VARIANTS    = {v["variant_id"]: v["multiplier"] for v in DATA["variants"]}

@bot.event
async def on_ready():
    print(f"\n✅ Logged in as {bot.user}")
    try:
        await bot.tree.sync()
        print("🔄 Slash commands synced")
    except Exception as e:
        print(f"⚠️ Sync error: {e}")
    
    # Check for active weather immediately on startup
    if weather_channel_id:
        await check_new_weather(is_restart=True)
    
    # Start background tasks
    fetch_updates.start()
    update_active_events.start()
    frequent_checks.start()
    print("🚀 Background tasks started")

# New task for frequent checks (every 20 seconds)
@tasks.loop(seconds=20)
async def frequent_checks():
    """Check for new weather and announcements every 20 seconds"""
    print("\n⏳ Running 20-second checks...")
    if weather_channel_id:
        await check_new_weather()
    if announcement_channel_id:
        await check_new_announcements()
    print("⏳ 20-second checks completed")

# Time Ago Helper (UTC based)
def time_ago(ts: float) -> str:
    now = datetime.now(timezone.utc).timestamp()
    diff = int(now - ts)
    if diff < 60:
        return f"{diff} second{'s' if diff != 1 else ''} ago"
    if diff < 3600:
        m = diff // 60
        return f"{m} minute{'s' if m != 1 else ''} ago"
    if diff < 86400:
        h = diff // 3600
        return f"{h} hour{'s' if h != 1 else ''} ago"
    d = diff // 864) else raw
                    print("📦 Received stock API data")
                else:
                    text = await r.text()
                    print(f"⚠️ Stock API returned non-JJSON: {text[:200]}")
                    return
        except Exception as e:
            print(f"⚠️ Stock API Error: {e}")
            return

        # Unified stock categories
        stock_categories = [
            ("seed_stock", seed_channel_id, "Seeds 🌱", "seed"),
            ("gear_stock", gear_channel_id, "Gear ⚙️", "gear"),
            ("egg_stock", egg_channel_id, "Eggs 🥚", "egg"),
            ("cosmetic_stock", cosmetic_channel_id, "Cosmetics 💄", "cosmetic"),
            ("eventshop_stock", event_stock_channel_id, "Event Stock 🎉", "event_stock"),
        ]
        
        for api_key, chan_id, title, state_key in stock_categories:
            if not chan_id:
                continue
                
            items = stock.get(api_key, [])
            if items:
                # Get timestamps from API response
                start_ts = max(i.get("start_date_unix", 0) for i in items)
                end_ts = max(i.get("end_date_unix", 0) for i in items)
                
                if start_ts > last_state.get(state_key, 0):
                    embed = create_stock_embed(items, title, start_ts, end_ts)
                    ch = bot.get_channel(chan_id)
                    if ch:
                        msg = await ch.send(embed=embed, view=create_invite_view())
                        print(f"✅ Sent new {state_key} stock to channel {chan_id}")
                        # Track for updates
                        active_events["stock"][state_key] = {
                            "message_id": msg.id,
                            "channel_id": chan_id,
                            "start_ts": start_ts,
                            "end_ts": end_ts,
                            "items": items,
                            "title": title
                        }
                    last_state[state_key] = start_ts
                else:
                    print(f"⏩ No new stock for {state_key}")

        # Jandel Announcement
        raw_note = stock.get("notification", [])
        note = raw_note[0] if isinstance(raw_note, list) and raw_note else None
        if note and isinstance(note, dict):
            msg_content = note.get("message")
            ts  = note.get("timestamp", 0)
            if msg_content and ts > last_state.get("announcement", 0):
                embed = discord.Embed(
                    title="📝 Jandel Announcement",
                    description=msg_content,
                    color=discord.Color.orange()
                )
                
                # Add time information
                embed.add_field(name="🕒 Posted", value=f"{time_ago(ts)}", inline=False)
                
                # Add end time if available
                end_ts = note.get("end_timestamp")
                if end_ts:
                    now = datetime.now(timezone.utc).timestamp()
                    if end_ts > now:
                        remaining = end_ts - now
                        mins = int(remaining // 60)
                        secs = int(remaining % 60)
                        embed.add_field(
                            name="⏱️ Ends In", 
                            value=f"{mins}m {secs}s", 
                            inline=True
                        )
                
                ch = bot.get_channel(announcement_channel_id)
                if ch:
                    msg = await ch.send(embed=embed, view=create_invite_view())
                    print(f"✅ Sent new announcement to channel {announcement_channel_id}")
                    # Track for updates
                    active_events["announcements"][ts] = {
                        "message_id": msg.id,
                        "channel_id": announcement_channel_id,
                        "start_ts": ts,
                        "end_ts": end_ts,
                        "content": msg_content
                    }
                last_state["announcement"] = ts
            else:
                print("⏩ No new announcements found")

        # Weather events (handled in dedicated function)
        if weather_channel_id:
            await check_new_weather()

        save_last_state()
        print("✅ 5-minute checks completed")

# Update active events every 5 seconds (faster countdown)
@tasks.loop(seconds=5)
async def update_active_events():
    current_utc = datetime.now(timezone.utc).timestamp()
    
    # Update stock events
    for key, event in list(active_events["stock"].items()):
        try:
            channel = bot.get_channel(event["channel_id"])
            if channel:
                message = await channel.fetch_message(event["message_id"])
                
                # Create updated embed
                embed = create_stock_embed(
                    event["items"], 
                    event["title"], 
                    event["start_ts"], 
                    event["end_ts"]
                )
                
                # Only update if the end time hasn't passed
                if event["end_ts"] > current_utc:
                    await message.edit(embed=embed)
                else:
                    # Remove expired event
                    del active_events["stock"][key]
                    print(f"⏩ Removed expired stock event: {key}")
                    
                    # Trigger immediate check for new stock
                    channel_id = get_channel_for_category(key)
                    if channel_id and key in STOCK_CATEGORY_MAPPING:
                        api_key, title = STOCK_CATEGORY_MAPPING[key]
                        asyncio.create_task(
                            check_new_stock_for_category(key, api_key, channel_id, title)
                        )
        except discord.NotFound:
            # Message was deleted, remove from tracking
            del active_events["stock"][key]
            print(f"⚠️ Stock message not found, removing: {key}")
        except Exception as e:
            print(f"⚠️ Error updating stock event: {e}")
    
    # Update weather events
    for wid, event in list(active_events["weather"].items()):
        try:
            channel = bot.get_channel(event["channel_id"])
            if channel:
                message = await channel.fetch_message(event["message_id"])
                
                # Create updated embed
                embed = create_weather_embed(event["weather"])
                
                # Check if event is still active
                w = event["weather"]
                start_ts = w.get("start_duration_unix",()
                )
                
                # Add time information
                embed.add_field(name="🕒 Posted", value=f"{time_ago(event['start_ts'])}", inline=False)
                
                # Add end time if available
                if event["end_ts"]:
                    now = current_utc
                    if event["end_ts"] > now:
                        remaining = event["end_ts"] - now
                        mins = int(remaining // 60)
                        secs = int(remaining % 60)
                        embed.add_field(
                            name="⏱️ Ends In", 
                            value=f"{mins}m {secs}s", 
                            inline=True
                        )
                
                # Only update if the end time hasn't passed
                if not event["end_ts"] or event["end_ts"] > now:
                    await message.edit(embed=embed)
                else:
                    # Remove expired announcement
                    del active_events["announcements"][key]
                    print(f"⏩ Removed expired announcement: {key}")
                    
                    # Trigger immediate check for new announcements
                    if announcement_channel_id:
                        asyncio.create_task(check_new_announcements())
        except discord.NotFound:
            del active_events["announcements"][key]
            print(f"⚠️ Announcement message not found, removing: {key}")
        except Exception as e:
            print(f"⚠️ Error updating announcement: {e}")

# Slash command: calculate item value
@bot.tree.command(name="calculate", description="Calculate Grow a Garden item value")
@app_commands.describe(
    item_name="Name or ID of the item",
    weight="Weight of the item",
    mutation="Mutation type (normal/mutated/ultra)",
    variant="Variant type (normal/rare/legendary)"
)
async def calculate(
    interaction: discord.Interaction,
    item_name: str,
    weight: float,
    mutation: str = "normal",
    variant: str = "normal"
):
    item_name = item_name.lower()
    fruit = next(
        (f for f in FRUIT_DATA
         if f["item_id"] == item_name or f["display_name"].lower() == item_name),
        None
    )
    if not fruit:
        await interaction.response.send_message(f"❌ Item '{item_name}' not found.", ephemeral=True)
        return

    mut_mult = MUTATIONS.get(mutation.lower(), 1)
    var_mult = VARIANTS.get(variant.lower(), 1)
    base = fruit["baseValue"]
    div  = fruit["weightDivisor"]
    value = round(base * (weight / div) * mut_mult * var_mult, 2)

    embed = discord.Embed(title="🍇 Item Value Calculator", color=discord.Color.purple())
    embed.add_field(name="Item", value=fruit["display_name"], inline=True)
    embed.add_field(name="Weight", value=weight, inline=True)
    embed.add_field(name="Mutation", value=mutation.title(), inline=True)
    embed.add_field(name="Variant", value=variant.title(), inline=True)
    embed.add_field(name="Calculated Value", value=f"${value:,.2f}", inline=False)

    await interaction.response.send_message(embed=embed)

# Admin-only decorator
def admin_only():
    async def predicate(ctx):
        if ctx.author.guild_permissions.administrator:
            return True
        await ctx.send("❌ Admin only.", delete_after=10)
        return False
    return commands.check(predicate)

# Admin channel setter commands
@bot.command(name="setseed")  
@admin_only()
async def set_seed(ctx):
    global seed_channel_id
    seed_channel_id = ctx.channel.id
    save_channels()
    await ctx.send(f"✅ Seed stock in {ctx.channel.mention}")

@bot.command(name="setgear")  
@admin_only()
async def set_gear(ctx):
    global gear_channel_id
    gear_channel_id = ctx.channel.id
    save_channels()
    await ctx.send(f"✅ Gear stock in {ctx.channel.mention}")

@bot.command(name="setegg")  
@admin_only()
async def set_egg(ctx):
    global egg_channel_id
    egg_channel_id = ctx.channel.id
    save_channels()
    await ctx.send(f"✅ Egg stock in {ctx.channel.mention}")

@bot.command(name="setcosmetic")
@admin_only()
async def set_cosmetic(ctx):
    global cosmetic_channel_id
    cosmetic_channel_id = ctx.channel.id
    save_channels()
    await ctx.send(f"✅ Cosmetic stock in {ctx.channel.mention}")

@bot.command(name="seteventstock")
@admin_only()
async def set_event_stock(ctx):
    global event_stock_channel_id
    event_stock_channel_id = ctx.channel.id
    save_channels()
    await ctx.send(f"✅ Event stock in {ctx.channel.mention}")

@bot.command(name="setannounce")
@admin_only()
async def set_announce(ctx):
    global announcement_channel_id
    announcement_channel_id = ctx.channel.id
    save_channels()
    await ctx.send(f"✅ Announcements in {ctx.channel.mention}")

@bot.command(name="setweather")
@admin_only()
async def set_weather(ctx):
    global weather_channel_id
    weather_channel_id = ctx.channel.id
    save_channels()
    await ctx.send(f"✅ Weather in {ctx.channel.mention}")

# Run the bot
bot.run(TOKEN)