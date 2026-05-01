import ast
import json
import os
import logging
from typing import Dict, List, Any, Optional, Set
from collections import defaultdict
from dataclasses import dataclass, asdict

logger = logging.getLogger("knowledge_graph")

# PART 1: Graph Data Model (CORE)
class NodeTypes:
    FILE = "File"
    FUNCTION = "Function"
    CLASS = "Class"
    MODULE = "Module"
    ERROR = "Error"
    FIX = "Fix"
    TASK = "Task"
    PATTERN = "Pattern"

class EdgeTypes:
    IMPORTS = "imports" # file -> module
    CALLS = "calls" # function -> function
    DEFINES = "defines" # file -> function/class
    DEPENDS_ON = "depends_on" # task -> file/module
    CAUSED_ERROR = "caused_error" # code -> error
    FIXED_BY = "fixed_by" # error -> fix
    SIMILAR_TO = "similar_to" # task -> task

@dataclass
class Node:
    id: str
    type: str
    properties: Dict[str, Any]

@dataclass
class Edge:
    source_id: str
    target_id: str
    type: str
    properties: Dict[str, Any]

# PART 2: Graph Storage
class KnowledgeGraph:
    def __init__(self, db_path: str = "knowledge_graph.json"):
        self.db_path = db_path
        self.nodes: Dict[str, Node] = {}
        self.edges: List[Edge] = []
        self.adjacency: Dict[str, List[Edge]] = defaultdict(list)
        self.load()

    def add_node(self, node_id: str, node_type: str, properties: Dict[str, Any] = None) -> Node:
        if node_id not in self.nodes:
            self.nodes[node_id] = Node(id=node_id, type=node_type, properties=properties or {})
        else:
            self.nodes[node_id].properties.update(properties or {})
        return self.nodes[node_id]

    def add_edge(self, source_id: str, target_id: str, edge_type: str, properties: Dict[str, Any] = None):
        if source_id not in self.nodes or target_id not in self.nodes:
            logger.warning(f"Cannot add edge {source_id} -> {target_id}: Missing nodes")
            return
            
        edge = Edge(source_id, target_id, edge_type, properties or {})
        self.edges.append(edge)
        self.adjacency[source_id].append(edge)
        
    def query_graph(self, node_id: str = None, edge_type: str = None) -> List[Edge]:
        """PART 8: Query System Core"""
        results = []
        if node_id:
            for e in self.adjacency.get(node_id, []):
                if not edge_type or e.type == edge_type:
                    results.append(e)
        else:
            for e in self.edges:
                if not edge_type or e.type == edge_type:
                    results.append(e)
        return results
        
    def get_related(self, node_id: str, depth: int = 1) -> Set[str]:
        """PART 8: Query System Related Nodes"""
        visited = set()
        queue = [(node_id, 0)]
        while queue:
            current, d = queue.pop(0)
            if current not in visited and d <= depth:
                visited.add(current)
                for edge in self.adjacency.get(current, []):
                    queue.append((edge.target_id, d + 1))
        return visited

    def save(self):
        data = {
            "nodes": {k: asdict(v) for k, v in self.nodes.items()},
            "edges": [asdict(e) for e in self.edges]
        }
        with open(self.db_path, 'w') as f:
            json.dump(data, f, indent=2)

    def load(self):
        if os.path.exists(self.db_path):
            try:
                with open(self.db_path, 'r') as f:
                    data = json.load(f)
                    for k, v in data.get("nodes", {}).items():
                        self.nodes[k] = Node(**v)
                    for e in data.get("edges", []):
                        edge = Edge(**e)
                        self.edges.append(edge)
                        self.adjacency[edge.source_id].append(edge)
            except Exception as e:
                logger.error(f"Error loading graph: {e}")

# PART 3: Code Indexing Engine
class CodeIndexer:
    def __init__(self, graph: KnowledgeGraph):
        self.graph = graph

    def index_file(self, filepath: str):
        if not os.path.exists(filepath):
            return
        
        file_node = self.graph.add_node(filepath, NodeTypes.FILE, {"path": filepath})
        
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
            tree = ast.parse(content)
            
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) or isinstance(node, ast.AsyncFunctionDef):
                    func_id = f"{filepath}::{node.name}"
                    self.graph.add_node(func_id, NodeTypes.FUNCTION, {"name": node.name})
                    self.graph.add_edge(filepath, func_id, EdgeTypes.DEFINES)
                    
                elif isinstance(node, ast.ClassDef):
                    class_id = f"{filepath}::{node.name}"
                    self.graph.add_node(class_id, NodeTypes.CLASS, {"name": node.name})
                    self.graph.add_edge(filepath, class_id, EdgeTypes.DEFINES)
                    
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        mod_id = f"module::{alias.name}"
                        self.graph.add_node(mod_id, NodeTypes.MODULE, {"name": alias.name})
                        self.graph.add_edge(filepath, mod_id, EdgeTypes.IMPORTS)
                        
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        mod_id = f"module::{node.module}"
                        self.graph.add_node(mod_id, NodeTypes.MODULE, {"name": node.module})
                        self.graph.add_edge(filepath, mod_id, EdgeTypes.IMPORTS)
                        
        except Exception as e:
            logger.warning(f"Failed to parse {filepath}: {e}")

# PART 4 & 6: Error + Fix Tracking & Pattern Learning
class ExperienceTracker:
    def __init__(self, graph: KnowledgeGraph):
        self.graph = graph

    def record_failure(self, task_id: str, code: str, error_msg: str, fix_applied: str = None):
        error_id = f"error::{hash(error_msg)}"
        self.graph.add_node(error_id, NodeTypes.ERROR, {"msg": error_msg})
        
        task_node = self.graph.add_node(task_id, NodeTypes.TASK, {"name": task_id})
        self.graph.add_edge(task_id, error_id, EdgeTypes.CAUSED_ERROR)
        
        if fix_applied:
            fix_id = f"fix::{hash(fix_applied)}"
            self.graph.add_node(fix_id, NodeTypes.FIX, {"code": fix_applied})
            self.graph.add_edge(error_id, fix_id, EdgeTypes.FIXED_BY)
            
        # Basic pattern detection
        if "ImportError" in error_msg or "ModuleNotFoundError" in error_msg:
            pattern_id = "pattern::missing_dependency"
            self.graph.add_node(pattern_id, NodeTypes.PATTERN, {"desc": "Missing Dependency"})
            self.graph.add_edge(error_id, pattern_id, EdgeTypes.SIMILAR_TO)

# PART 5: Context Retrieval Engine
class ContextEngine:
    def __init__(self, graph: KnowledgeGraph):
        self.graph = graph

    def retrieve_context(self, task: str) -> Dict[str, Any]:
        task_id = f"task::{hash(task)}"
        related = self.graph.get_related(task_id, depth=2)
        
        return {
            "related_files": [n for n in related if n in self.graph.nodes and self.graph.nodes[n].type == NodeTypes.FILE],
            "known_errors": [n for n in related if n in self.graph.nodes and self.graph.nodes[n].type == NodeTypes.ERROR],
            "known_fixes": [n for n in related if n in self.graph.nodes and self.graph.nodes[n].type == NodeTypes.FIX]
        }
