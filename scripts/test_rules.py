#!/usr/bin/env python3
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from scan_mirror_palindromes import is_palindrome
from scan_mirror_palindromes_proof import eligible_name_key, language_from_key

def test(name,expected_norm,expected_len,expected_pal=True):
    pal,norm,gs=is_palindrome(name)
    assert norm==expected_norm,(name,norm,expected_norm)
    assert len(gs)==expected_len,(name,len(gs),expected_len)
    assert pal==expected_pal,(name,pal,expected_pal)

test('OWOMOMOWO','OWOMOMOWO',9)
test('Umolo-Olomu','UmoloOlomu',10)
test('A man a plan a canal Panama','AmanaplanacanalPanama',21,True)
test('Ellemelle','Ellemelle',9)
test('Not-a-palindrome','Notapalindrome',14,False)

# Bare locked keys remain eligible.
for key in ('name','official_name','loc_name','alt_name','short_name','int_name','nat_name','reg_name'):
    assert eligible_name_key(key), key

# Legitimate language/script variants remain eligible.
for key,lang in (
    ('name:en','en'),
    ('name:sr-Latn','sr-Latn'),
    ('name:zh-Hant','zh-Hant'),
    ('official_name:pt_BR','pt-BR'),
    ('alt_name:de','de'),
):
    assert eligible_name_key(key), key
    assert language_from_key(key)==lang,(key,language_from_key(key),lang)

# Metadata/history-like name:* keys are not names in a language variant and
# must not enter the proof scan.
for key in (
    'name:source',
    'name:etymology',
    'name:historic',
    'name:old',
    'name:2020',
    'name:pronunciation',
    'name:en:source',
    'official_name:source',
    'alt_name:historic',
):
    assert not eligible_name_key(key), key

print('rule tests passed')
