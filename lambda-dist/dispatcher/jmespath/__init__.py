# Copyright (c) 2025 filynai.com
# Author: Bin Lee
# Email: blee@filynai.com

from jmespath import parser
from jmespath.visitor import Options

__version__ = '1.0.1'


def compile(expression):
    return parser.Parser().parse(expression)


def search(expression, data, options=None):
    return parser.Parser().parse(expression).search(data, options=options)
