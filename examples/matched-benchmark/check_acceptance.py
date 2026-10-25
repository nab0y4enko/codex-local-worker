"""Frozen independent probes; run from candidate checkout with slug or stats argument."""
import random
import string
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))
task = sys.argv[1]
rng = random.Random(25)
alphabet = string.ascii_letters + string.digits + ' _\t\n—、♥é𐐀𐐨'
values = [''.join(rng.choice(alphabet) for _ in range(rng.randrange(80))) for _ in range(200)]

if task == 'slug':
    from textkit.slug import slugify
    cases = [('', ''), ('  ', ''), ('Hello, WORLD!', 'hello-world'),
             ('Crème brûlée', 'creme-brulee'), ('a___b---c', 'a-b-c'),
             ('123 !! 456', '123-456'), ('你好', ''), ('a—b', 'ab')]
    for value, expected in cases:
        assert slugify(value) == expected, (value, slugify(value), expected)
    for value in values:
        assert slugify(slugify(value)) == slugify(value), value
elif task == 'stats':
    from textkit.stats import word_frequencies
    cases = [('', {}), ('___ !!!', {}),
             ('Hello, hello WORLD!', {'hello': 2, 'world': 1}),
             ('hello—world、again', {'hello': 1, 'world': 1, 'again': 1}),
             ('a♥b', {'a': 1, 'b': 1}), ('𐐀 𐐨', {'𐐨': 2}),
             ("don't can't won't", {'don': 1, 't': 3, 'can': 1, 'won': 1}),
             ('café CAFÉ 123_45', {'café': 2, '123': 1, '45': 1})]
    for value, expected in cases:
        assert word_frequencies(value) == expected, (value, word_frequencies(value), expected)

    def reference_words(value):
        result, word = {}, ''
        for char in value.lower() + ' ':
            if char.isalnum():
                word += char
            elif word:
                result[word] = result.get(word, 0) + 1
                word = ''
        return result

    for value in values:
        assert word_frequencies(value) == reference_words(value), value
    first = word_frequencies('a a')
    first['a'] = 99
    assert word_frequencies('a a') == {'a': 2}
else:
    raise SystemExit('expected slug or stats')
print('%s: 8 explicit cases and 200 seeded probes passed%s' %
      (task, '; mutation independence passed' if task == 'stats' else ''))
