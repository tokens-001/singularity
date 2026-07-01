"""Business logic layer - manages TODO items."""
from typing import List, Dict, Any, Optional
from storage import Storage


class TodoManager:
    """Manages TODO items with CRUD operations."""
    
    def __init__(self, storage: Storage):
        self.storage = storage
        self.items: List[Dict[str, Any]] = []
        self._load()
    
    def _load(self) -> None:
        """Load items from storage."""
        self.items = self.storage.load()
    
    def _save(self) -> None:
        """Save items to storage."""
        self.storage.save(self.items)
    
    def add(self, title: str) -> Dict[str, Any]:
        """Add a new TODO item."""
        item_id = max([item.get('id', 0) for item in self.items], default=0) + 1
        item = {
            'id': item_id,
            'title': title,
            'completed': False
        }
        self.items.append(item)
        self._save()
        return item
    
    def list_all(self) -> List[Dict[str, Any]]:
        """List all TODO items."""
        return self.items
    
    def get(self, item_id: int) -> Optional[Dict[str, Any]]:
        """Get a specific TODO item by ID."""
        for item in self.items:
            if item.get('id') == item_id:
                return item
        return None
    
    def update(self, item_id: int, title: Optional[str] = None, completed: Optional[bool] = None) -> Optional[Dict[str, Any]]:
        """Update a TODO item."""
        item = self.get(item_id)
        if not item:
            return None
        
        if title is not None:
            item['title'] = title
        if completed is not None:
            item['completed'] = completed
        
        self._save()
        return item
    
    def delete(self, item_id: int) -> bool:
        """Delete a TODO item."""
        initial_count = len(self.items)
        self.items = [item for item in self.items if item.get('id') != item_id]
        
        if len(self.items) < initial_count:
            self._save()
            return True
        return False
    
    def toggle(self, item_id: int) -> Optional[Dict[str, Any]]:
        """Toggle completion status of a TODO item."""
        item = self.get(item_id)
        if not item:
            return None
        
        item['completed'] = not item.get('completed', False)
        self._save()
        return item
