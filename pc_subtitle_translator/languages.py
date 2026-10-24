"""Convenient choices, not a promise of equal model quality for every language."""

LANGUAGES = [
    ("af", "Afrikaans"), ("ar", "Arabic"), ("hy", "Armenian"), ("az", "Azerbaijani"),
    ("be", "Belarusian"), ("bn", "Bengali"), ("bs", "Bosnian"), ("bg", "Bulgarian"),
    ("ca", "Catalan"), ("zh", "Chinese"), ("hr", "Croatian"), ("cs", "Czech"),
    ("da", "Danish"), ("nl", "Dutch"), ("en", "English"), ("et", "Estonian"),
    ("fi", "Finnish"), ("fr", "French"), ("gl", "Galician"), ("de", "German"),
    ("el", "Greek"), ("he", "Hebrew"), ("hi", "Hindi"), ("hu", "Hungarian"),
    ("is", "Icelandic"), ("id", "Indonesian"), ("it", "Italian"), ("ja", "Japanese"),
    ("kn", "Kannada"), ("kk", "Kazakh"), ("ko", "Korean"), ("lv", "Latvian"),
    ("lt", "Lithuanian"), ("mk", "Macedonian"), ("ms", "Malay"), ("mr", "Marathi"),
    ("ne", "Nepali"), ("no", "Norwegian"), ("fa", "Persian"), ("pl", "Polish"),
    ("pt", "Portuguese"), ("ro", "Romanian"), ("ru", "Russian"), ("sr", "Serbian"),
    ("sk", "Slovak"), ("sl", "Slovenian"), ("es", "Spanish"), ("sw", "Swahili"),
    ("sv", "Swedish"), ("tl", "Tagalog"), ("ta", "Tamil"), ("te", "Telugu"),
    ("th", "Thai"), ("tr", "Turkish"), ("uk", "Ukrainian"), ("ur", "Urdu"),
    ("vi", "Vietnamese"), ("cy", "Welsh"),
]

TARGET_LANGUAGES = [name for _, name in LANGUAGES] + [
    "Spanish (Puerto Rico)", "Spanish (Latin America)", "Portuguese (Brazil)",
    "Chinese (Simplified)", "Chinese (Traditional)",
]


def language_name(code: str) -> str:
    return dict(LANGUAGES).get(code, code) if code else "automatic detection"
