"""确认 ttk.Treeview 的 <<TreeviewSelect>> 会不会被“重新选中同一项”再次触发。"""
import os
import sys
import tkinter as tk
from tkinter import ttk

mode = sys.argv[1] if len(sys.argv) > 1 else "restore"
state = {"calls": 0}


def on_select(_event=None):
    state["calls"] += 1
    if state["calls"] >= 30:
        print(f"[{mode}] STORM：处理器被反复触发 {state['calls']} 次")
        sys.stdout.flush()
        os._exit(0)
    if mode == "restore":
        tree.selection_set("a")
    elif mode == "guarded" and tree.selection() != ("a",):
        tree.selection_set("a")


def finish():
    print(f"[{mode}] 未成环，处理器共触发 {state['calls']} 次")
    sys.stdout.flush()
    os._exit(0)


root = tk.Tk()
root.withdraw()
tree = ttk.Treeview(root, columns=("name",), show="headings", selectmode="browse")
for name in ("a", "b"):
    tree.insert("", "end", iid=name, values=(name,))
tree.pack()
tree.bind("<<TreeviewSelect>>", on_select)
tree.selection_set("b")
root.after(2000, finish)
root.mainloop()
