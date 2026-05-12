"""
services/t9.py
המרת לחיצות T9 לטקסט (לכתובות מייל)

מיפוי מקשים:
2=abc  3=def  4=ghi  5=jkl  6=mno  7=pqrs  8=tuv  9=wxyz  0=.@-_
* = מעבר לאות הבאה באותו מקש (כמו T9 רגיל)
# = סיום

דוגמה: gmail.com = 44 * 6 * 33 * 555 * 0 * 222 * 666 * 6
"""

T9_MAP = {
    '2': 'abc',
    '3': 'def',
    '4': 'ghi',
    '5': 'jkl',
    '6': 'mno',
    '7': 'pqrs',
    '8': 'tuv',
    '9': 'wxyz',
    '0': '.@-_',
}


def t9_to_text(digits: str) -> str:
    """
    Convert T9 digit sequence to text.
    '*' advances to next letter on same key.
    '#' ends input.
    Example: '44*6*33*555*0*222*666*6' -> 'gmail.com'
    """
    result = []
    digits = digits.replace('#', '')
    groups = digits.split('*')

    for group in groups:
        if not group:
            result.append('')
            continue
        key = group[0]
        count = len(group)
        if key in T9_MAP:
            letters = T9_MAP[key]
            index = (count - 1) % len(letters)
            result.append(letters[index])
        else:
            result.append(group)

    return ''.join(result)


def text_to_t9_hint(text: str) -> str:
    """
    Helper: given an email, show user what to press.
    Useful for UI/documentation.
    """
    reverse = {}
    for digit, letters in T9_MAP.items():
        for i, letter in enumerate(letters):
            reverse[letter] = digit * (i + 1)

    parts = []
    for char in text.lower():
        if char in reverse:
            parts.append(reverse[char])
        else:
            parts.append(char)
    return ' * '.join(parts)


if __name__ == '__main__':
    # Quick test
    examples = [
        ('44*6*33*555*0*222*666*6', 'gmail.com'),
        ('0', '@'),
        ('666*8*555*0*222*666*6', 'oul.com'),
    ]
    for digits, expected in examples:
        result = t9_to_text(digits)
        status = 'OK' if result == expected else 'FAIL'
        print(f"{status}: {digits} -> '{result}' (expected '{expected}')")
