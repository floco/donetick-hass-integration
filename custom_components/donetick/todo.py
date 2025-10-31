"""Todo for Donetick integration."""
from __future__ import annotations
import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.todo import (
    TodoItem,
    TodoItemStatus,
    TodoListEntity,
    TodoListEntityFeature, 
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    DOMAIN,
    CONF_URL,
    CONF_TOKEN,
    CONF_SHOW_DUE_IN,
    CONF_CREATE_UNIFIED_LIST,
    CONF_CREATE_ASSIGNEE_LISTS,
    CONF_CREATE_LABEL_LISTS,
    CONF_REFRESH_INTERVAL,
    DEFAULT_REFRESH_INTERVAL,
)
from .api import DonetickApiClient
from .model import DonetickTask, DonetickMember

_LOGGER = logging.getLogger(__name__)


def _extract_label_id(label: dict[str, Any]) -> str | None:
    """Return a string identifier for a label object."""

    if not isinstance(label, dict):
        return None

    for key in ("id", "labelId", "label_id"):
        if (value := label.get(key)) is not None:
            return str(value)

    return None

async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Donetick todo platform."""
    session = async_get_clientsession(hass)
    client = DonetickApiClient(
        hass.data[DOMAIN][config_entry.entry_id][CONF_URL],
        hass.data[DOMAIN][config_entry.entry_id][CONF_TOKEN],
        session,
    )

    refresh_interval_seconds = config_entry.data.get(CONF_REFRESH_INTERVAL, DEFAULT_REFRESH_INTERVAL)
    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name="donetick_todo",
        update_method=client.async_get_tasks,
        update_interval=timedelta(seconds=refresh_interval_seconds),
    )

    await coordinator.async_config_entry_first_refresh()

    if coordinator.data is None:
        _LOGGER.debug("Coordinator returned no tasks on first refresh")
    else:
        _LOGGER.debug(
            "Coordinator returned %d tasks on first refresh", len(coordinator.data)
        )

    entities: list[DonetickTodoListBase] = []

    # Create unified list if enabled (check options first, then data)
    create_unified = config_entry.options.get(CONF_CREATE_UNIFIED_LIST, config_entry.data.get(CONF_CREATE_UNIFIED_LIST, True))
    if create_unified:
        entity = DonetickAllTasksList(coordinator, config_entry)
        entity._circle_members = []  # Will be set after we get members
        entities.append(entity)

    label_definitions: dict[str, dict[str, Any]] = {}
    if coordinator.data:
        for task in coordinator.data:
            labels = getattr(task, "labels_v2", []) or []
            _LOGGER.debug(
                "Task %s (%s) exposes %d labels_v2 entries: %s",
                getattr(task, "id", "unknown"),
                getattr(task, "name", "<unnamed>"),
                len(labels),
                labels,
            )
            for label in labels:
                label_id = _extract_label_id(label)
                if label_id is None or label_id in label_definitions:
                    continue
                label_definitions[label_id] = label

    # Get circle members for all entities (useful for custom cards)
    circle_members = []
    try:
        circle_members = await client.async_get_circle_members()
        _LOGGER.debug("Found %d circle members", len(circle_members))
        
        # Set circle members on unified entity if it exists
        if entities and hasattr(entities[0], '_circle_members'):
            entities[0]._circle_members = circle_members
            
    except Exception as e:
        _LOGGER.error("Failed to get circle members: %s", e)
    
    # Create per-assignee lists if enabled (check options first, then data)
    create_assignee_lists = config_entry.options.get(CONF_CREATE_ASSIGNEE_LISTS, config_entry.data.get(CONF_CREATE_ASSIGNEE_LISTS, False))
    if create_assignee_lists:
        _LOGGER.debug("Assignee lists enabled in config")
        for member in circle_members:
            if member.is_active:
                _LOGGER.debug("Creating entity for member: %s (ID: %d)", member.display_name, member.user_id)
                entity = DonetickAssigneeTasksList(coordinator, config_entry, member)
                entity._circle_members = circle_members
                entities.append(entity)
    else:
        _LOGGER.debug("Assignee lists not enabled in config")

    create_label_lists = config_entry.options.get(
        CONF_CREATE_LABEL_LISTS, config_entry.data.get(CONF_CREATE_LABEL_LISTS, False)
    )
    if create_label_lists and label_definitions:
        _LOGGER.debug("Label lists enabled in config; creating %d label entities", len(label_definitions))
        for label in label_definitions.values():
            entity = DonetickLabelTasksList(coordinator, config_entry, label)
            entity._circle_members = circle_members
            entities.append(entity)
    elif create_label_lists:
        _LOGGER.debug(
            "Label lists enabled but no labels were discovered from coordinator data"
        )
        if not coordinator.data:
            _LOGGER.debug("Coordinator data is empty; nothing to inspect for labels")
        else:
            for task in coordinator.data:
                _LOGGER.debug(
                    "Task %s labels_v2 payload at failure: %s",
                    getattr(task, "id", "unknown"),
                    getattr(task, "labels_v2", []),
                )

    _LOGGER.debug("Creating %d total entities", len(entities))
    async_add_entities(entities)

# Remove old assignee detection function since we now use circle members

class DonetickTodoListBase(CoordinatorEntity, TodoListEntity):
    """Base class for Donetick Todo List entities."""
    
    _attr_supported_features = (
        TodoListEntityFeature.CREATE_TODO_ITEM | 
        TodoListEntityFeature.UPDATE_TODO_ITEM |
        TodoListEntityFeature.DELETE_TODO_ITEM |
        TodoListEntityFeature.SET_DESCRIPTION_ON_ITEM |
        TodoListEntityFeature.SET_DUE_DATE_ON_ITEM |
        TodoListEntityFeature.SET_DUE_DATETIME_ON_ITEM
    )

    def __init__(self, coordinator: DataUpdateCoordinator, config_entry: ConfigEntry) -> None:
        """Initialize the Todo List."""
        super().__init__(coordinator)
        self._config_entry = config_entry

    def _filter_tasks(self, tasks):
        """Filter tasks based on entity type. Override in subclasses."""
        return tasks

    @property
    def todo_items(self) -> list[TodoItem] | None: 
        """Return a list of todo items."""
        if self.coordinator.data is None:
            return None
        
        filtered_tasks = self._filter_tasks(self.coordinator.data)
        return [
            TodoItem(
                summary=task.name,
                uid="%s--%s" % (task.id, task.next_due_date),
                status=self.get_status(task.next_due_date, task.is_active),
                due=task.next_due_date,
                description=task.description or ""
            ) for task in filtered_tasks if task.is_active
        ]

    def get_status(self, due_date: datetime, is_active: bool) -> TodoItemStatus:
        """Return the status of the task."""
        if not is_active:
            return TodoItemStatus.COMPLETED
        return TodoItemStatus.NEEDS_ACTION
    
    @property
    def extra_state_attributes(self):
        """Return additional state attributes for custom cards."""
        attributes = {
            "config_entry_id": self._config_entry.entry_id,
            "donetick_url": self._config_entry.data[CONF_URL],
        }
        
        # Add circle members data for custom card user selection
        if hasattr(self, '_circle_members'):
            attributes["circle_members"] = [
                {
                    "user_id": member.user_id,
                    "display_name": member.display_name,
                    "username": member.username,
                }
                for member in self._circle_members
            ]
        
        return attributes

    async def async_create_todo_item(self, item: TodoItem) -> None:
        """Create a todo item."""
        session = async_get_clientsession(self.hass)
        client = DonetickApiClient(
            self._config_entry.data[CONF_URL],
            self._config_entry.data[CONF_TOKEN],
            session,
        )
        
        try:
            # Determine the created_by user for assignee lists
            created_by = None
            if hasattr(self, '_member'):
                created_by = self._member.user_id
            
            # Convert due date to RFC3339 format if provided
            due_date = None
            if item.due:
                due_date = item.due.isoformat()
            
            result = await client.async_create_task(
                name=item.summary,
                description=item.description,
                due_date=due_date,
                created_by=created_by
            )
            _LOGGER.info("Created task '%s' with ID %d", item.summary, result.id)
            
        except Exception as e:
            _LOGGER.error("Failed to create task '%s': %s", item.summary, e)
            raise
        
        await self.coordinator.async_refresh()

    async def async_update_todo_item(self, item: TodoItem, context = None) -> None:
        """Update a todo item."""
        _LOGGER.debug("Update todo item: %s %s", item.uid, item.status)
        if not self.coordinator.data:
            return None
        
        session = async_get_clientsession(self.hass)
        client = DonetickApiClient(
            self._config_entry.data[CONF_URL],
            self._config_entry.data[CONF_TOKEN],
            session,
        )
        
        task_id = int(item.uid.split("--")[0])
        
        try:
            if item.status == TodoItemStatus.COMPLETED:
                # Complete the task
                _LOGGER.debug("Completing task %s", item.uid)
                # Determine who should complete this task using smart logic
                completed_by = await self._get_completion_user_id(client, item, context)
                
                res = await client.async_complete_task(task_id, completed_by)
                if res.frequency_type != "once":
                    _LOGGER.debug("Task %s is recurring, updating next due date", res.name)
                    item.status = TodoItemStatus.NEEDS_ACTION
                    item.due = res.next_due_date
                    self.async_update_todo_item(item)
            else:
                # Update task properties (summary, description, due date)
                _LOGGER.debug("Updating task %d properties", task_id)
                
                # Convert due date to RFC3339 format if provided
                due_date = None
                if item.due:
                    due_date = item.due.isoformat()
                
                await client.async_update_task(
                    task_id=task_id,
                    name=item.summary,
                    description=item.description,
                    due_date=due_date
                )
                _LOGGER.info("Updated task %d", task_id)
                
        except Exception as e:
            _LOGGER.error("Error updating task %d: %s", task_id, e)
            raise
        
        await self.coordinator.async_refresh()

    async def async_delete_todo_items(self, uids: list[str]) -> None:
        """Delete todo items."""
        session = async_get_clientsession(self.hass)
        client = DonetickApiClient(
            self._config_entry.data[CONF_URL],
            self._config_entry.data[CONF_TOKEN],
            session,
        )
        
        for uid in uids:
            try:
                task_id = int(uid.split("--")[0])
                success = await client.async_delete_task(task_id)
                if success:
                    _LOGGER.info("Deleted task %d", task_id)
                else:
                    _LOGGER.error("Failed to delete task %d", task_id)
                    
            except Exception as e:
                _LOGGER.error("Error deleting task %s: %s", uid, e)
                raise
        
        await self.coordinator.async_refresh()
    
    async def _get_completion_user_id(self, client, item, context=None) -> int | None:
        """Determine who should complete this task using smart logic."""
        
        # Option 1: Context-based completion
        # If this is an assignee-specific list, use that assignee
        if hasattr(self, '_member'):
            _LOGGER.debug("Using assignee from specific list: %s (ID: %d)", self._member.display_name, self._member.user_id)
            return self._member.user_id
        
        # If completing from "All Tasks", find the task's original assignee
        task_id = int(item.uid.split("--")[0])
        if self.coordinator.data:
            for task in self.coordinator.data:
                if task.id == task_id and task.assigned_to:
                    _LOGGER.debug("Using task's original assignee: %d", task.assigned_to)
                    return task.assigned_to
        
        # No default user - rely on context-based or task assignee
        
        _LOGGER.debug("No completion user determined, using default")
        return None

class DonetickAllTasksList(DonetickTodoListBase):
    """Donetick All Tasks List entity."""

    def __init__(self, coordinator: DataUpdateCoordinator, config_entry: ConfigEntry) -> None:
        """Initialize the All Tasks List."""
        super().__init__(coordinator, config_entry)
        self._attr_unique_id = f"dt_{config_entry.entry_id}_all_tasks"
        self._attr_name = "All Tasks"

    def _filter_tasks(self, tasks):
        """Return all active tasks."""
        return [task for task in tasks if task.is_active]

class DonetickAssigneeTasksList(DonetickTodoListBase):
    """Donetick Assignee-specific Tasks List entity."""

    def __init__(self, coordinator: DataUpdateCoordinator, config_entry: ConfigEntry, member: DonetickMember) -> None:
        """Initialize the Assignee Tasks List."""
        super().__init__(coordinator, config_entry)
        self._member = member
        self._attr_unique_id = f"dt_{config_entry.entry_id}_{member.user_id}_tasks"
        self._attr_name = f"{member.display_name}'s Tasks"

    def _filter_tasks(self, tasks):
        """Return tasks assigned to this member."""
        return [task for task in tasks if task.is_active and task.assigned_to == self._member.user_id]


class DonetickLabelTasksList(DonetickTodoListBase):
    """Donetick label-specific Tasks List entity."""

    def __init__(self, coordinator: DataUpdateCoordinator, config_entry: ConfigEntry, label: dict[str, Any]) -> None:
        """Initialize the label-specific Tasks List."""

        super().__init__(coordinator, config_entry)
        self._label = label
        self._label_id = _extract_label_id(label)
        label_name = label.get("name") if isinstance(label, dict) else None
        if not label_name:
            label_name = f"Label {self._label_id}" if self._label_id else "Label"

        suffix = self._label_id or label_name.replace(" ", "_")
        self._attr_unique_id = f"dt_{config_entry.entry_id}_label_{suffix}_tasks"
        self._attr_name = f"{label_name} Tasks"

    def _filter_tasks(self, tasks):
        """Return tasks associated with this label."""

        def _task_has_label(task: DonetickTask) -> bool:
            for task_label in getattr(task, "labels_v2", []) or []:
                if _extract_label_id(task_label) == self._label_id:
                    return True
            return False

        return [task for task in tasks if task.is_active and _task_has_label(task)]

    @property
    def extra_state_attributes(self):
        """Return additional attributes, including label metadata."""

        attrs = dict(super().extra_state_attributes or {})
        label_details = {}

        if isinstance(self._label, dict):
            for key in ("id", "labelId", "label_id", "name", "color", "description"):
                if (value := self._label.get(key)) is not None:
                    label_details[key] = value

        if self._label_id and "label_id" not in label_details:
            label_details["label_id"] = self._label_id

        if label_details:
            attrs["label"] = label_details

        return attrs

# Keep the old class for backward compatibility
class DonetickTodoListEntity(DonetickAllTasksList):
    """Donetick Todo List entity."""
    
    """Legacy Donetick Todo List entity for backward compatibility."""
    
    def __init__(self, coordinator: DataUpdateCoordinator, config_entry: ConfigEntry) -> None:
        """Initialize the Todo List."""
        super().__init__(coordinator, config_entry)
        self._attr_unique_id = f"dt_{config_entry.entry_id}"

