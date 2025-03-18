#!/usr/bin/env python
import tkinter as tk
from tkinter import ttk, messagebox
import json
from ttkbootstrap import Style

# Global dictionary for notes and a variable for current note title.
notes = {}
current_note_title = None

# Load saved notes from JSON.
def load_notes_from_file():
    global notes
    try:
        with open("notes.json", "r") as f:
            notes = json.load(f)
    except FileNotFoundError:
        notes = {}

# Save notes dictionary to file.
def save_notes_to_file():
    with open("notes.json", "w") as f:
        json.dump(notes, f, indent=4)

# Update the sidebar Treeview with the note titles.
def update_treeview():
    tree.delete(*tree.get_children())
    for title in sorted(notes.keys()):
        tree.insert("", tk.END, iid=title, text=title)

# When a note is selected in the sidebar, load it into the editor.
def on_tree_select(event):
    global current_note_title
    selected = tree.selection()
    if not selected:
        return
    title = selected[0]
    current_note_title = title
    # Set the two editor fields.
    title_entry.delete(0, tk.END)
    title_entry.insert(0, title)
    content_text.delete("1.0", tk.END)
    content_text.insert(tk.END, notes.get(title, ""))

# Create a new (empty) note to edit.
def new_note():
    global current_note_title
    current_note_title = None
    title_entry.delete(0, tk.END)
    content_text.delete("1.0", tk.END)
    # Also clear Treeview selection.
    tree.selection_remove(tree.selection())

# Save/update the current note.
def save_note():
    global current_note_title
    title = title_entry.get().strip()
    content = content_text.get("1.0", tk.END).rstrip()
    if not title:
        messagebox.showerror("Error", "Title cannot be empty!")
        return

    # If renaming an existing note, remove the old key.
    if current_note_title and current_note_title != title:
        notes.pop(current_note_title, None)

    notes[title] = content
    current_note_title = title
    save_notes_to_file()
    update_treeview()
    # Optionally, select the updated note in the sidebar.
    tree.selection_set(title)

# Delete the currently loaded note.
def delete_note():
    global current_note_title
    if not current_note_title:
        messagebox.showerror("Error", "No note selected to delete!")
        return
    confirm = messagebox.askyesno("Delete Note", f"Are you sure you want to delete '{current_note_title}'?")
    if confirm:
        notes.pop(current_note_title, None)
        save_notes_to_file()
        update_treeview()
        new_note()  # clear the editor

# -------------------------------
# Create the main window
root = tk.Tk()
root.title("Notes App with Sidebar")
root.geometry("700x500")

# Use ttkbootstrap style.
style = Style(theme='journal')

# Create two main frames:
#   sidebar_frame for the note titles in a Treeview,
#   editor_frame for editing note content.
sidebar_frame = ttk.Frame(root, padding=(10, 10))
sidebar_frame.pack(side=tk.LEFT, fill=tk.Y)

editor_frame = ttk.Frame(root, padding=(10, 10))
editor_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

# Sidebar: Add a label and the Treeview.
sidebar_label = ttk.Label(sidebar_frame, text="Saved Notes", font=("TkDefaultFont", 12, "bold"))
sidebar_label.pack(pady=(0, 5))

tree = ttk.Treeview(sidebar_frame, show="tree", height=20)
tree.pack(fill=tk.Y, expand=True)

# Bind selection event to load note.
tree.bind("<<TreeviewSelect>>", on_tree_select)

# Editor frame: Create fields for note title and content.
title_label = ttk.Label(editor_frame, text="Title:")
title_label.grid(row=0, column=0, padx=5, pady=5, sticky="W")

title_entry = ttk.Entry(editor_frame, width=50)
title_entry.grid(row=0, column=1, padx=5, pady=5, sticky="EW")

content_label = ttk.Label(editor_frame, text="Content:")
content_label.grid(row=1, column=0, padx=5, pady=5, sticky="NW")

content_text = tk.Text(editor_frame, wrap="word", width=50, height=20)
content_text.grid(row=1, column=1, padx=5, pady=5, sticky="NSEW")

# Make the editor frame expandable.
editor_frame.columnconfigure(1, weight=1)
editor_frame.rowconfigure(1, weight=1)

# Add the control buttons in a separate frame at the bottom of the editor.
button_frame = ttk.Frame(editor_frame)
button_frame.grid(row=2, column=1, pady=10, sticky="E")

new_button = ttk.Button(button_frame, text="New Note", command=new_note, style="info.TButton")
new_button.pack(side=tk.LEFT, padx=(0, 5))

save_button = ttk.Button(button_frame, text="Save", command=save_note, style="secondary.TButton")
save_button.pack(side=tk.LEFT, padx=(0, 5))

delete_button = ttk.Button(button_frame, text="Delete", command=delete_note, style="primary.TButton")
delete_button.pack(side=tk.LEFT)

# -------------------------------
# On startup, load notes and populate the sidebar.
load_notes_from_file()
update_treeview()

root.mainloop()