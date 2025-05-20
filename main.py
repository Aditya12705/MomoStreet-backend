from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import sqlite3
import csv
import os
import pandas as pd
from datetime import datetime
import pytz
import requests
import urllib.parse
import json

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/img", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "..", "img")), name="img")

DB_PATH = "orders.db"
MENU_PATH = "menu.csv"
MENU_XLSX_PATH = "menu.xlsx"
MENU_JSON_PATH = "menu.json"

# --- Database setup ---
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cur = conn.cursor()
cur.execute("""
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    items TEXT,
    name TEXT,
    phone TEXT
)
""")
# Add order_history table for persistent history
cur.execute("""
CREATE TABLE IF NOT EXISTS order_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    items TEXT,
    name TEXT,
    phone TEXT,
    created_at TIMESTAMP
)
""")
conn.commit()

# --- Ensure created_at column exists ---
def ensure_created_at_column():
    cur.execute("PRAGMA table_info(orders)")
    columns = [row[1] for row in cur.fetchall()]
    if "created_at" not in columns:
        # Add the column without default
        cur.execute("ALTER TABLE orders ADD COLUMN created_at TIMESTAMP")
        conn.commit()
        # Set current timestamp for existing rows
        cur.execute("UPDATE orders SET created_at = datetime('now') WHERE created_at IS NULL")
        conn.commit()

ensure_created_at_column()

# --- Models ---
class Order(BaseModel):
    items: str
    name: str
    phone: str

class MenuItem(BaseModel):
    id: int
    name: str
    price: float

# --- Helper: get image url for a food item using local img/ folder or fallback to Unsplash ---
def get_food_image_url(item_name):
    img_folder = os.path.join(os.path.dirname(__file__), "..", "img")
    candidates = [
        item_name,
        item_name.replace(" ", "_"),
        item_name.replace("_", " "),
        item_name.lower(),
        item_name.lower().replace(" ", "_"),
        item_name.lower().replace("_", " "),
    ]
    exts = [".jpg", ".jpeg", ".png", ".webp"]
    try:
        files = os.listdir(img_folder)
    except Exception as e:
        print(f"DEBUG: Could not list img folder: {e}")
        files = []
    # Normalize all file names for robust matching
    normalized_files = [
        (f, f.strip().lower()) for f in files
        if os.path.isfile(os.path.join(img_folder, f))
    ]
    found = False
    for base in candidates:
        for ext in exts:
            expected = f"{base}{ext}".strip().lower()
            for orig_file, norm_file in normalized_files:
                if norm_file == expected:
                    print(f"Matched image for '{item_name}': {orig_file}")
                    found = True
                    return f"http://localhost:8000/img/{urllib.parse.quote(orig_file)}"
    if not found:
        print(f"No local image found for '{item_name}', using Unsplash fallback.")
    query = item_name.replace(" ", "+")
    return f"https://source.unsplash.com/400x300/?{query},food"

# --- Helper: parse extras string to extraOptions ---
def parse_extra_options(extras):
    options = []
    if not extras:
        return options
    if "cheese burst" in extras.lower():
        import re
        reg = re.search(r"Regular\s*-\s*Rs\.\s*(\d+)", extras, re.I)
        med = re.search(r"Medium\s*-\s*Rs\.\s*(\d+)", extras, re.I)
        if reg:
            options.append({"name": "Cheese Burst (Regular)", "price": int(reg.group(1))})
        if med:
            options.append({"name": "Cheese Burst (Medium)", "price": int(med.group(1))})
    elif "add cheese" in extras.lower():
        import re
        m = re.search(r"Add Cheese\s*Rs\s*(\d+)", extras, re.I)
        if m:
            options.append({"name": "Add Cheese", "price": int(m.group(1))})
    return options

# --- Menu helpers ---
def group_menu(flat_menu):
    """Convert flat menu list to grouped structure for frontend."""
    from collections import defaultdict
    grouped = defaultdict(list)
    pizza_subgroups = defaultdict(list)  # pizzaSubcategory -> [items]
    for item in flat_menu:
        cat = item.get("category", "Menu")
        pizza_subcat = item.get("pizzaSubcategory", "")
        # Group pizza items only if category is PIZZA and has sizes
        if cat and cat.upper() == "PIZZA" and item.get("sizes") and len(item["sizes"]) > 0:
            pizza_subgroups[pizza_subcat or "Other"].append(item)
        else:
            grouped[cat].append(item)
    menu = []
    # Add normal groups (except PIZZA)
    for cat, items in grouped.items():
        if cat.upper() != "PIZZA":
            menu.append({"subcategory": cat, "items": items})
    # Add pizza group (with subgroups)
    if pizza_subgroups:
        menu.append({
            "subcategory": "PIZZA",
            "items": [
                {"subcategory": subcat, "items": items}
                for subcat, items in pizza_subgroups.items()
            ]
        })
    return menu

def load_menu():
    # 1. If menu.json exists, load from it (admin-edited, flat)
    if os.path.exists(MENU_JSON_PATH):
        with open(MENU_JSON_PATH, encoding="utf-8") as f:
            flat = json.load(f)
        return group_menu(flat)
    # Try to load from Excel first
    if os.path.exists(MENU_XLSX_PATH):
        try:
            df = pd.read_excel(MENU_XLSX_PATH, header=None)
            menu = []
            current_subcat = None
            subcat_items = []
            item_id = 1

            # Pizza-specific state
            pizza_mode = False
            pizza_groups = []
            pizza_subsubcat = None
            pizza_subsubcat_items = []
            pizza_size_headers = []

            for idx, row in df.iterrows():
                name = str(row[0]).strip() if not pd.isna(row[0]) else ""
                extras = str(row[1]).strip() if len(row) > 1 and not pd.isna(row[1]) else ""
                price1 = row[2] if len(row) > 2 and not pd.isna(row[2]) else None
                price2 = row[3] if len(row) > 3 and not pd.isna(row[3]) else None

                # Debug: Log each row being processed
                print(f"DEBUG: Processing row {idx}: name={name}, extras={extras}, price1={price1}, price2={price2}")

                # --- Detect subcategory/header (e.g., "PIZZA", "MAGGI") ---
                if name and name.isupper() and (str(price1).lower() in ["price", "nan", ""] or price1 is None):
                    # Save previous pizza group or normal group
                    if pizza_mode:
                        if pizza_subsubcat and pizza_subsubcat_items:
                            pizza_groups.append({
                                "subcategory": pizza_subsubcat,
                                "items": pizza_subsubcat_items
                            })
                            print(f"DEBUG: Added pizza subgroup '{pizza_subsubcat}' with items: {pizza_subsubcat_items}")
                        if current_subcat and pizza_groups:
                            menu.append({
                                "subcategory": current_subcat,
                                "items": pizza_groups
                            })
                            print(f"DEBUG: Added pizza category '{current_subcat}' with groups: {pizza_groups}")
                        pizza_mode = False
                        pizza_groups = []
                        pizza_subsubcat = None
                        pizza_subsubcat_items = []
                        pizza_size_headers = []
                    elif current_subcat and subcat_items:
                        menu.append({
                            "subcategory": current_subcat,
                            "items": subcat_items
                        })
                        print(f"DEBUG: Added category '{current_subcat}' with items: {subcat_items}")
                    current_subcat = name
                    subcat_items = []
                    # Enable pizza mode if this is PIZZA
                    if name.lower() == "pizza":
                        pizza_mode = True
                        pizza_groups = []
                        pizza_subsubcat = None
                        pizza_subsubcat_items = []
                        pizza_size_headers = []
                        print("DEBUG: Entered pizza mode")
                    continue

                # --- Detect pizza sub-subcategory (e.g., "VEG", "CHICKEN") and size headers ---
                if pizza_mode and name and name.isupper() and price1 and price2:
                    # This row has size headers in price1 and price2 (e.g., "Regular (7")", "Medium (10")")
                    if pizza_subsubcat and pizza_subsubcat_items:
                        pizza_groups.append({
                            "subcategory": pizza_subsubcat,
                            "items": pizza_subsubcat_items
                        })
                        print(f"DEBUG: Added pizza subgroup '{pizza_subsubcat}' with items: {pizza_subsubcat_items}")
                    pizza_subsubcat = name
                    pizza_subsubcat_items = []
                    # Set size headers from price1 and price2
                    pizza_size_headers = []
                    if price1 and str(price1).strip().lower() not in ["nan", ""]:
                        pizza_size_headers.append(str(price1).strip())
                    if price2 and str(price2).strip().lower() not in ["nan", ""]:
                        pizza_size_headers.append(str(price2).strip())
                    print(f"DEBUG: Detected pizza subcategory '{pizza_subsubcat}' with size headers: {pizza_size_headers}")
                    continue

                # --- Pizza item ---
                if pizza_mode and pizza_subsubcat and name and price1 not in ["Price", None, "nan"]:
                    options = []
                    if pizza_size_headers:
                        if price1 not in [None, "nan", ""]:
                            try:
                                options.append({
                                    "size": pizza_size_headers[0],
                                    "price": float(price1),
                                })
                            except ValueError as e:
                                print(f"DEBUG: Failed to parse price1 '{price1}' for '{name}': {e}")
                        if len(pizza_size_headers) > 1 and price2 not in [None, "nan", ""]:
                            try:
                                options.append({
                                    "size": pizza_size_headers[1],
                                    "price": float(price2),
                                })
                            except ValueError as e:
                                print(f"DEBUG: Failed to parse price2 '{price2}' for '{name}': {e}")
                    else:
                        # Fallback if size headers are not set
                        if price1 not in [None, "nan", ""]:
                            try:
                                options.append({
                                    "size": "Regular",
                                    "price": float(price1),
                                })
                            except ValueError as e:
                                print(f"DEBUG: Failed to parse price1 '{price1}' for '{name}': {e}")
                        if price2 not in [None, "nan", ""]:
                            try:
                                options.append({
                                    "size": "Medium",
                                    "price": float(price2),
                                })
                            except ValueError as e:
                                print(f"DEBUG: Failed to parse price2 '{price2}' for '{name}': {e}")
                    if options:  # Only add item if it has valid sizes
                        pizza_subsubcat_items.append({
                            "id": item_id,
                            "name": name,
                            "extras": extras if extras and extras.lower() != "nan" else "",
                            "sizes": options,
                            "image": get_food_image_url(name),
                            "extraOptions": parse_extra_options(extras)
                        })
                        print(f"DEBUG: Added pizza item '{name}' with sizes: {options}")
                        item_id += 1
                    else:
                        print(f"DEBUG: Skipped pizza item '{name}' due to no valid sizes")
                    continue

                # --- Non-pizza item ---
                if not pizza_mode and name and price1 not in ["Price", None, "nan"]:
                    try:
                        price_val = float(price1)
                    except Exception:
                        price_val = None
                    if price_val is not None:
                        subcat_items.append({
                            "id": item_id,
                            "name": name,
                            "extras": extras if extras and extras.lower() != "nan" else "",
                            "price": price_val,
                            "image": get_food_image_url(name),
                            "extraOptions": parse_extra_options(extras)
                        })
                        print(f"DEBUG: Added non-pizza item '{name}' with price: {price_val}")
                        item_id += 1
                    continue

            # --- Add last subcategory or pizza group ---
            if pizza_mode:
                if pizza_subsubcat and pizza_subsubcat_items:
                    pizza_groups.append({
                        "subcategory": pizza_subsubcat,
                        "items": pizza_subsubcat_items
                    })
                    print(f"DEBUG: Added final pizza subgroup '{pizza_subsubcat}' with items: {pizza_subsubcat_items}")
                if current_subcat and pizza_groups:
                    menu.append({
                        "subcategory": current_subcat,
                        "items": pizza_groups
                    })
                    print(f"DEBUG: Added final pizza category '{current_subcat}' with groups: {pizza_groups}")
            elif current_subcat and subcat_items:
                menu.append({
                    "subcategory": current_subcat,
                    "items": subcat_items
                })
                print(f"DEBUG: Added final category '{current_subcat}' with items: {subcat_items}")

            # Debug print to verify pizza section
            print("DEBUG: Final menu structure:", menu)
            return menu
        except Exception as e:
            print(f"Error loading menu.xlsx: {e}")
            # Fallback to CSV or demo menu
            return load_fallback_menu()

    # Fallback to CSV or demo menu
    return load_fallback_menu()

def load_fallback_menu():
    menu = []
    if os.path.exists(MENU_PATH):
        with open(MENU_PATH, newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("id") and row.get("name") and row.get("price"):
                    try:
                        # Use image from CSV if present, otherwise fallback to local/unsplash
                        image = row.get("image")
                        if not image or image.strip() == "":
                            image = get_food_image_url(row["name"])
                        menu.append({
                            "id": int(row["id"]),
                            "name": row["name"],
                            "price": float(row["price"]),
                            "image": image,
                            "extraOptions": parse_extra_options(row.get("extras", ""))
                        })
                    except Exception:
                        continue
    if not menu:
        menu = [
            {"id": 1, "name": "Plain Maggi", "price": 49, "image": get_food_image_url("Plain Maggi")},
            {"id": 2, "name": "Butter Maggi", "price": 59, "image": get_food_image_url("Butter Maggi")},
        ]
    # Wrap fallback in a single category for frontend compatibility
    return [{"subcategory": "Menu", "items": menu}]

def save_menu(flat_menu):
    # Save the flat menu as JSON (admin source of truth)
    with open(MENU_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(flat_menu, f, ensure_ascii=False, indent=2)
    # Optionally, also save to CSV for backup (not used for loading anymore)
    # with open(MENU_PATH, "w", newline='', encoding='utf-8') as f:
    #     writer = csv.DictWriter(f, fieldnames=["id", "name", "price", "image"])
    #     writer.writeheader()
    #     for item in flat_menu:
    #         writer.writerow({
    #             "id": item.get("id"),
    #             "name": item.get("name"),
    #             "price": item.get("price"),
    #             "image": item.get("image", ""),
    #         })

def flatten_menu(menu):
    """Flatten the menu structure for admin editing/export."""
    flat = []
    for group in menu:
        items = group.get("items", [])
        # Pizza section: items is a list of subgroups
        if items and isinstance(items[0], dict) and "subcategory" in items[0] and "items" in items[0]:
            for subgroup in items:
                for item in subgroup.get("items", []):
                    flat.append({
                        "id": item.get("id"),
                        "name": item.get("name"),
                        "extras": item.get("extras", ""),
                        "price": None,  # Pizza items use sizes, not price
                        "sizes": item.get("sizes", []),
                        "image": item.get("image", ""),
                        "extraOptions": item.get("extraOptions", []),
                        "pizzaSubcategory": subgroup.get("subcategory", ""),
                        "category": group.get("subcategory", ""),
                    })
        else:
            for item in items:
                flat.append({
                    "id": item.get("id"),
                    "name": item.get("name"),
                    "extras": item.get("extras", ""),
                    "price": item.get("price"),
                    "sizes": item.get("sizes", []),
                    "image": item.get("image", ""),
                    "extraOptions": item.get("extraOptions", []),
                    "pizzaSubcategory": "",
                    "category": group.get("subcategory", ""),
                })
    return flat

# --- API Endpoints ---
@app.get("/menu")
def get_menu():
    return load_menu()

# Debug endpoint to verify images in menu
@app.get("/menu/debug")
def get_menu_debug():
    menu = load_menu()
    # Flatten all items and show name + image only
    debug_items = []
    for cat in menu:
        if isinstance(cat.get("items"), list):
            for item in cat.get("items", []):
                if "subcategory" in item:  # Pizza subgroup
                    for sub_item in item.get("items", []):
                        debug_items.append({"name": sub_item.get("name"), "image": sub_item.get("image")})
                else:
                    debug_items.append({"name": item.get("name"), "image": item.get("image")})
    return debug_items

@app.get("/img/debug")
def img_debug():
    img_folder = os.path.join(os.path.dirname(__file__), "..", "img")
    try:
        files = os.listdir(img_folder)
    except Exception:
        files = []
    # Only show image files
    exts = (".jpg", ".jpeg", ".png", ".webp")
    images = [
        {
            "filename": f,
            "url": f"/img/{urllib.parse.quote(f)}"
        }
        for f in files if f.lower().endswith(exts)
    ]
    return images

@app.post("/admin/upload-image")
async def upload_image(file: UploadFile = File(...)):
    img_folder = os.path.join(os.path.dirname(__file__), "..", "img")
    os.makedirs(img_folder, exist_ok=True)
    filename = file.filename
    # Ensure unique filename
    base, ext = os.path.splitext(filename)
    i = 1
    while os.path.exists(os.path.join(img_folder, filename)):
        filename = f"{base}_{i}{ext}"
        i += 1
    file_path = os.path.join(img_folder, filename)
    with open(file_path, "wb") as f:
        f.write(await file.read())
    url = f"http://localhost:8000/img/{urllib.parse.quote(filename)}"
    return {"url": url}

@app.post("/order")
def place_order(order: Order):
    cur.execute(
        "INSERT INTO orders (items, name, phone, created_at) VALUES (?, ?, ?, datetime('now'))",
        (order.items, order.name, order.phone)
    )
    conn.commit()
    return {"status": "success"}

@app.get("/admin/orders")
def get_orders():
    cur.execute("SELECT id, items, name, phone, created_at FROM orders ORDER BY id DESC")
    rows = cur.fetchall()
    return [
        {"id": r[0], "items": r[1], "name": r[2], "phone": r[3], "created_at": r[4]}
        for r in rows
    ]

@app.post("/admin/clear")
def clear_orders():
    # Move all current orders to order_history before deleting
    cur.execute("INSERT INTO order_history (items, name, phone, created_at) SELECT items, name, phone, created_at FROM orders")
    cur.execute("DELETE FROM orders")
    conn.commit()
    return {"status": "cleared"}

@app.get("/admin/history")
def get_order_history():
    cur.execute("SELECT id, items, name, phone, created_at FROM order_history ORDER BY id DESC")
    rows = cur.fetchall()
    ist = pytz.timezone("Asia/Kolkata")
    result = []
    for r in rows:
        # Parse UTC time and convert to IST, then format as dd/mm/yyyy HH:MM:SS
        try:
            # Try parsing as ISO format, fallback to as-is if fails
            dt = datetime.strptime(r[4], "%Y-%m-%d %H:%M:%S")
            dt_ist = pytz.utc.localize(dt).astimezone(ist)
            formatted = dt_ist.strftime("%d/%m/%Y %H:%M:%S")
        except Exception:
            formatted = r[4]
        result.append({
            "id": r[0],
            "items": r[1],
            "name": r[2],
            "phone": r[3],
            "created_at": formatted
        })
    return result

@app.post("/admin/menu")
def update_menu(menu: list[dict]):
    # Save the menu as provided (including admin-updated images)
    save_menu(menu)
    return {"status": "menu updated"}

@app.get("/admin/export-menu")
def export_menu():
    """
    Export the current menu as a flat JSON array for admin editing.
    Each item includes all fields: id, name, extras, price, sizes, image, extraOptions, pizzaSubcategory, category.
    """
    menu = load_menu()
    flat = flatten_menu(menu)
    return flat