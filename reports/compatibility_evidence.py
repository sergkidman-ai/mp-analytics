# поток: rev
"""Absence from a product list is not evidence of incompatibility."""
import re

DENIAL = re.compile(r'не\s+(?:подойд\w*|подход\w*|совместим\w*|предназначен\w*)|несовместим\w*',re.I)
ABSENCE = re.compile(r'(?:нет|отсутств\w*|не\s+(?:значит\w*|указан\w*|перечислен\w*|включ[её]н\w*))'
                     r'[^.!?]{0,90}(?:списк\w*|переч\w*|карточк\w*)|'
                     r'(?:списк\w*|переч\w*|карточк\w*)[^.!?]{0,90}'
                     r'(?:не\s+(?:значит\w*|указан\w*|перечислен\w*)|отсутств\w*|нет\b)',re.I)


def absence_denial(reply):
    text=reply or ''
    return bool(DENIAL.search(text) and ABSENCE.search(text))
