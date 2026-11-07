"""Default categories and keyword-based auto-categorization."""
from __future__ import annotations

DEFAULT_CATEGORIES: dict[str, list[tuple[str, str, list[str]]]] = {
    "expense": [
        ("Groceries", "🛒", ["grocery", "groceries", "market", "supermarket", "migros", "bim", "a101",
                             "carrefour", "lidl", "aldi", "walmart", "costco", "emart", "homeplus",
                             "milk", "bread", "eggs", "fruit", "vegetables", "meat",
                             "korzinka", "makro", "havas", "bozor", "sut", "non", "olma", "go'sht", "tuxum",
                             "sabzavot", "мясо", "молоко", "хлеб", "продукты", "магнит", "пятерочка"]),
        ("Eating out", "🍽", ["restaurant", "lunch", "dinner", "breakfast", "cafe", "coffee",
                              "starbucks", "pizza", "burger", "kebab", "delivery", "yemeksepeti",
                              "getir", "baemin", "mcdonalds", "kfc", "food", "snack", "tea",
                              "tushlik", "kafe", "oshxona", "evos", "osh", "somsa", "choy", "kofe",
                              "обед", "кафе", "ресторан", "кофе", "wolt", "uzum tezkor"]),
        ("Transport", "🚕", ["taxi", "uber", "bus", "metro", "subway", "train", "fuel", "gas",
                             "petrol", "parking", "toll", "bolt", "kakao", "transport", "car",
                             "taksi", "yandex go", "benzin", "metan", "propan", "avtobus", "такси",
                             "бензин", "метро"]),
        ("Housing", "🏠", ["rent", "mortgage", "aidat", "maintenance", "furniture", "ikea", "repair"]),
        ("Utilities", "💡", ["electricity", "water", "internet", "phone", "mobile", "utility",
                             "utilities", "heating", "natural gas", "turkcell", "vodafone", "kt", "skt",
                             "uzmobile", "beeline", "ucell", "mobiuz", "humans", "elektr", "svet", "kommunal",
                             "свет", "газ", "интернет", "связь"]),
        ("Health", "💊", ["pharmacy", "doctor", "hospital", "medicine", "dentist", "clinic", "gym",
                          "fitness", "health"]),
        ("Shopping", "🛍", ["clothes", "shoes", "amazon", "trendyol", "hepsiburada", "coupang",
                            "zara", "h&m", "shopping", "electronics", "gift"]),
        ("Subscriptions", "🔁", ["netflix", "spotify", "youtube", "icloud", "subscription", "apple",
                                 "google one", "chatgpt", "claude", "disney", "prime"]),
        ("Entertainment", "🎬", ["cinema", "movie", "concert", "game", "games", "steam", "bar",
                                 "party", "ticket", "entertainment"]),
        ("Education", "📚", ["course", "book", "books", "tuition", "school", "university", "udemy",
                             "education", "exam"]),
        ("Travel", "✈️", ["flight", "hotel", "airbnb", "booking", "travel", "trip", "visa"]),
        ("Personal care", "🧴", ["haircut", "barber", "cosmetics", "shampoo", "salon", "care", "shampun",
                                 "sartarosh", "шампунь", "парикмахер"]),
        ("Fees & taxes", "🧾", ["fee", "fees", "tax", "commission", "fine", "penalty", "interest"]),
        ("Other", "📦", []),
    ],
    "income": [
        ("Salary", "💼", ["salary", "wage", "maas", "maaş", "월급", "payroll", "oylik",
                        "ish haqi", "зарплата", "аванс"]),
        ("Freelance", "🧑‍💻", ["freelance", "client", "project", "invoice"]),
        ("Investment income", "📈", ["dividend", "interest", "coupon", "profit"]),
        ("Gifts received", "🎁", ["gift", "present"]),
        ("Refunds", "↩️", ["refund", "cashback", "return"]),
        ("Other income", "💰", []),
    ],
}

FALLBACK = {"expense": "Other", "income": "Other income"}


def keyword_category(text: str, kind: str) -> str | None:
    """Best default-category name for text by keyword match, or None."""
    low = f" {text.lower()} "
    best, best_len = None, 0
    for name, _emoji, words in DEFAULT_CATEGORIES.get(kind, []):
        for w in words:
            if (f" {w} " in low or (len(w) >= 5 and w in low)) and len(w) > best_len:
                best, best_len = name, len(w)
    return best
