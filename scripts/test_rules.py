#!/usr/bin/env python3
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from scan_mirror_palindromes import is_palindrome

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
print('rule tests passed')
