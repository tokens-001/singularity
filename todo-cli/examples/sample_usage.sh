#!/bin/bash

# Sample usage script for todo-cli

echo "Adding some sample tasks..."
./target/debug/todo-cli add "Buy groceries"
./target/debug/todo-cli add "Walk the dog"
./target/debug/todo-cli add "Finish project documentation"

echo ""
echo "Listing all tasks:"
./target/debug/todo-cli list

echo ""
echo "Marking task #2 as completed..."
./target/debug/todo-cli complete 2

echo ""
echo "Listing all tasks after completing task #2:"
./target/debug/todo-cli list

echo ""
echo "Removing task #3..."
./target/debug/todo-cli remove 3

echo ""
echo "Final task list:"
./target/debug/todo-cli list