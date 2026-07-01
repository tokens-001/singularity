"""Tests for CLI interface."""

import json
import os

import pytest

from todo.cli import main


class TestCLIAdd:
    """Test CLI add command."""

    def test_add_basic(self, tmp_path, capsys):
        data_file = tmp_path / "todos.json"
        result = main(["-f", str(data_file), "add", "Buy milk"])
        
        assert result == 0
        captured = capsys.readouterr()
        assert "Added" in captured.out
        assert "Buy milk" in captured.out

    def test_add_with_description(self, tmp_path, capsys):
        data_file = tmp_path / "todos.json"
        result = main(["-f", str(data_file), "add", "Task", "-d", "Details"])
        
        assert result == 0

    def test_add_empty_title_fails(self, tmp_path, capsys):
        data_file = tmp_path / "todos.json"
        result = main(["-f", str(data_file), "add", ""])
        
        assert result == 1


class TestCLIList:
    """Test CLI list command."""

    def test_list_empty(self, tmp_path, capsys):
        data_file = tmp_path / "todos.json"
        result = main(["-f", str(data_file), "list"])
        
        assert result == 0
        captured = capsys.readouterr()
        assert "No todos" in captured.out

    def test_list_shows_todos(self, tmp_path, capsys):
        data_file = tmp_path / "todos.json"
        main(["-f", str(data_file), "add", "Task 1"])
        main(["-f", str(data_file), "add", "Task 2"])
        
        result = main(["-f", str(data_file), "list"])
        assert result == 0
        captured = capsys.readouterr()
        assert "Task 1" in captured.out
        assert "Task 2" in captured.out


class TestCLIDone:
    """Test CLI done command."""

    def test_done_marks_completed(self, tmp_path, capsys):
        data_file = tmp_path / "todos.json"
        main(["-f", str(data_file), "add", "Task"])
        
        # Get the ID prefix from add output
        captured = capsys.readouterr()
        # Extract ID from "Added: [xxxxxxxx] Task"
        id_prefix = captured.out.split("[")[1].split("]")[0]
        
        result = main(["-f", str(data_file), "done", id_prefix])
        assert result == 0
        assert "Completed" in capsys.readouterr().out


class TestCLIDelete:
    """Test CLI delete command."""

    def test_delete_removes_todo(self, tmp_path, capsys):
        data_file = tmp_path / "todos.json"
        main(["-f", str(data_file), "add", "Task"])
        
        captured = capsys.readouterr()
        id_prefix = captured.out.split("[")[1].split("]")[0]
        
        result = main(["-f", str(data_file), "delete", id_prefix])
        assert result == 0
        
        # Verify it's gone
        main(["-f", str(data_file), "list"])
        captured = capsys.readouterr()
        assert "No todos" in captured.out


class TestCLIShow:
    """Test CLI show command."""

    def test_show_json_output(self, tmp_path, capsys):
        data_file = tmp_path / "todos.json"
        main(["-f", str(data_file), "add", "Test task"])
        
        captured = capsys.readouterr()
        id_prefix = captured.out.split("[")[1].split("]")[0]
        
        result = main(["-f", str(data_file), "show", id_prefix])
        assert result == 0
        
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["title"] == "Test task"


class TestCLIClear:
    """Test CLI clear command."""

    def test_clear_deletes_all(self, tmp_path, capsys):
        data_file = tmp_path / "todos.json"
        main(["-f", str(data_file), "add", "Task 1"])
        main(["-f", str(data_file), "add", "Task 2"])
        
        result = main(["-f", str(data_file), "clear"])
        assert result == 0
        assert "Deleted 2" in capsys.readouterr().out
