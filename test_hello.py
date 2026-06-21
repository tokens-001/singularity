# -*- coding: utf-8 -*-

import hello


def test_hello_output(capsys):
    hello.main()
    captured = capsys.readouterr()
    assert captured.out == 'Hello 奇点\n'
