#!/bin/bash

# Sample usage of the todo-cli tool
echo "Adding some sample tasks..."
./target/debug/todo add "Buy groceries"
./target/debug/todo add "Walk the dog"
./target/debug/todo add "Read a book"

echo -e "\nListing all tasks:"
./target/debug/todo list

echo -e "\nMarking task #1 as completed:"
./target/debug/todo complete 1

echo -e "\nListing all tasks after completing task #1:"
./target/debug/todo list

echo -e "\nRemoving task #2:"
./target/debug/todo remove 2

echo -e "\nFinal task list:"
./target/debug/todo list