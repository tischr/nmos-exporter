import asyncio
import json
import logging
from typing import Dict, List, Optional, Any, Callable
from dataclasses import dataclass, replace
from enum import IntEnum
import websockets
from websockets.asyncio.client import ClientConnection


logger = logging.getLogger(__name__)


ROOT_BLOCK_OID = 1
NC_CLASS_MANAGER_CLASS_ID = [1, 3, 2]
NC_STATUS_MONITOR_CLASS_ID = [1, 2, 2]
NC_RECEIVER_MONITOR_CLASS_ID = [1, 2, 2, 1]
NC_SENDER_MONITOR_CLASS_ID = [1, 2, 2, 2]


def monitor_role(class_id: List[int]) -> Optional[str]:
    """Classify a monitor by its (possibly derived) class id."""
    if class_id[:len(NC_SENDER_MONITOR_CLASS_ID)] == NC_SENDER_MONITOR_CLASS_ID:
        return "sender"
    if class_id[:len(NC_RECEIVER_MONITOR_CLASS_ID)] == NC_RECEIVER_MONITOR_CLASS_ID:
        return "receiver"
    return None


class MessageType(IntEnum):
    """IS-12 Message Types"""
    COMMAND = 0
    COMMAND_RESPONSE = 1
    NOTIFICATION = 2
    SUBSCRIPTION = 3
    SUBSCRIPTION_RESPONSE = 4
    ERROR = 5


class ClassLevel(IntEnum):
    """IS-12 Class Levels"""
    NC_OBJECT = 1
    NC_BLOCK = 2
    NC_CLASS_MANAGER = 3
    NC_DEVICE_MANAGER = 4


class MethodId(IntEnum):
    """Method Indices (within the class level that defines the method)"""
    GET = 1                        
    SET = 2                        
    FIND_MEMBERS_BY_CLASS_ID = 4   
    GET_CLASS_DESCRIPTOR = 1       

@dataclass
class Property:
    """Represents a discovered property"""
    description: str
    id: Dict[str, int]
    name: str
    typeName: str
    isReadOnly: bool
    isNullable: bool
    isSequence: bool
    constraints: str
    isDeprecated: bool
    value: Optional[Any] = None

@dataclass
class BlockMember:
    """Represents a member of an NcBlock"""
    oid: int
    role: str
    class_id: List[int]
    user_label: str
    properties: List[Property]
    description: str = ""
    constant_oid: bool = True
    owner: int = 1


class IS12Client:
    """
    NMOS IS-12 Control Protocol Client
    
    Handles WebSocket communication, command/response correlation.
    """
    
    def __init__(self, ws_url: str):
        """
        Initialize IS-12 client
        
        Args:
            ws_url: WebSocket URL (e.g., 'ws://localhost:8080/x-nmos/control/v1.0')
        """
        self.ws_url = ws_url
        self.ws: Optional[ClientConnection] = None
        self._handle_counter = 0
        self._pending_requests: Dict[int, asyncio.Future] = {}
        self.on_notification: Optional[Callable[[int, Dict, int, Any], None]] = None
        self._subscribed_oids: set = set()
        self._subscription_future: Optional[asyncio.Future] = None
        self._subscription_lock = asyncio.Lock()
        self._receive_task: Optional[asyncio.Task] = None
        
    def is_connected(self) -> bool:
        """Check whether the WebSocket connection is open"""
        return self.ws is not None and getattr(self.ws, 'state', None) == 1

    async def connect(self):
        """Establish WebSocket connection"""
        if self.is_connected():
            logger.warning("WebSocket is already connected.")
            return

        logger.info(f"Connecting to {self.ws_url}")
        
        try:
            self.ws = await asyncio.wait_for(
                websockets.connect(self.ws_url), 
                timeout=10
            )
            
            self._receive_task = asyncio.create_task(self._receive_loop())
            logger.info("Connected successfully")

        except asyncio.TimeoutError:
            logger.error(f"Connection to {self.ws_url} timed out.")
            raise
        except (ConnectionRefusedError) as e:
            logger.error(f"Failed to connect: {e}")
            self.ws = None 
            raise
        except Exception as e:
            logger.error(f"An unexpected error occurred: {e}")
            raise
        
    async def disconnect(self):
        """Close WebSocket connection"""
        if self._receive_task:
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass
                
        if self.ws:
            await self.ws.close()
            logger.info("Disconnected")
            
    def _next_handle(self) -> int:
        """Generate next unique handle for request correlation"""
        self._handle_counter += 1
        return self._handle_counter
        
    async def _receive_loop(self):
        """Background task to receive and route messages"""
        try:
            async for message in self.ws:
                await self._handle_response(json.loads(message))
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Error in receive loop: {e}")
            try:
                await self.ws.close()
            except Exception:
                pass

    async def _handle_response(self, message: Dict):
        """Handle command responses, notifications and subscription responses"""
        message_type = message.get("messageType")

        if message_type == MessageType.NOTIFICATION:
            self._handle_notifications(message.get("notifications", []))
            return

        if message_type == MessageType.SUBSCRIPTION_RESPONSE:
            future = self._subscription_future
            if future and not future.done():
                future.set_result(message.get("subscriptions", []))
            return

        if message_type == MessageType.ERROR:
            logger.error(f"IS-12 protocol error: {message}")
            return

        responses = message.get("responses", [])

        for response in responses:
            handle = response.get("handle")
            if handle in self._pending_requests:
                future = self._pending_requests.pop(handle)
                if not future.cancelled():
                    future.set_result(response)

    def _handle_notifications(self, notifications: List[Dict]):
        """Route PropertyChanged notifications to the on_notification callback"""
        for notification in notifications:
            event_id = notification.get("eventId", {})
            if event_id != {"level": 1, "index": 1}:  # NcObject PropertyChanged
                logger.debug(f"Ignoring notification with eventId {event_id}")
                continue

            event_data = notification.get("eventData", {})
            change_type = event_data.get("changeType")
            if change_type != 0:  # only ValueChanged, no sequence changes
                logger.debug(f"Ignoring notification with changeType {change_type}")
                continue

            if not self.on_notification:
                continue

            try:
                self.on_notification(
                    notification.get("oid"),
                    event_data.get("propertyId"),
                    change_type,
                    event_data.get("value")
                )
            except Exception as e:
                logger.error(f"Error in notification callback: {e}")

    async def subscribe(self, oids: List[int], timeout: float = 10.0) -> List[int]:
        """
        Subscribe to property changed events for given oids

        Args:
            oids: Object ids to subscribe to
            timeout: Maximum time to wait for the subscription response

        Returns:
            List of oids the device confirmed subscription for
        """
        async with self._subscription_lock:
            self._subscribed_oids |= set(oids)

            message = {
                "messageType": MessageType.SUBSCRIPTION,
                "subscriptions": sorted(self._subscribed_oids)
            }

            self._subscription_future = asyncio.Future()
            await self.ws.send(json.dumps(message))

            try:
                confirmed = await asyncio.wait_for(self._subscription_future, timeout=timeout)
            except asyncio.TimeoutError:
                logger.error(f"Subscription request timed out after {timeout}s")
                raise
            finally:
                self._subscription_future = None

            logger.info(f"Subscribed to {len(confirmed)} objects")
            return confirmed
                    
    async def _send_command(self, commands: List[Dict], timeout: float = 10.0) -> List[Dict]:
        """
        Send command(s) and wait for response(s)
        
        Args:
            commands: List of command dictionaries
            timeout: Maximum time to wait for responses
            
        Returns:
            List of response dictionaries
        """
        # Assign handles and create futures
        futures = []
        for cmd in commands:
            handle = self._next_handle()
            cmd["handle"] = handle
            future = asyncio.Future()
            self._pending_requests[handle] = future
            futures.append(future)
            
        # Send message
        message = {
            "messageType": MessageType.COMMAND,
            "commands": commands
        }
      

        await self.ws.send(json.dumps(message))
        
        # Wait for all responses with timeout
        try:
            responses = await asyncio.wait_for(asyncio.gather(*futures), timeout=timeout)
            return responses
        except asyncio.TimeoutError:
            # Clean up pending requests on timeout
            for cmd in commands:
                self._pending_requests.pop(cmd["handle"], None)
            logger.error(f"Command timed out after {timeout}s")
            raise
    
    async def find_members_by_class_id(
        self,
        block_oid: int,
        class_id: List[int],
        include_derived: bool = True,
        recurse: bool = True,
    ) -> List[BlockMember]:
        """
        Find block members of a given control class using 2m4.

        Args:
            block_oid: Block to search from (use ROOT_BLOCK_OID for the whole model)
            class_id: Control class id to match
            include_derived: Also match subclasses of class_id
            recurse: Descend into nested blocks

        Returns:
            List of BlockMember objects (NcBlockMemberDescriptors)
        """
        commands = [{
            "oid": block_oid,
            "methodId": {
                "level": ClassLevel.NC_BLOCK,
                "index": MethodId.FIND_MEMBERS_BY_CLASS_ID
            },
            "arguments": {
                "classId": class_id,
                "includeDerived": include_derived,
                "recurse": recurse
            }
        }]

        responses = await self._send_command(commands)
        response = responses[0]

        if "error" in response:
            raise Exception(f"FindMembersByClassId failed: {response['error']}")

        members_data = response.get("result", {}).get("value", [])

        members = [
            BlockMember(
                oid=member_data.get("oid"),
                role=member_data.get("role", ""),
                class_id=member_data.get("classId", []),
                user_label=member_data.get("userLabel", ""),
                properties=[],
                description=member_data.get("description", ""),
                constant_oid=member_data.get("constantOid", True),
                owner=member_data.get("owner", block_oid)
            )
            for member_data in members_data
        ]

        logger.info(f"Found {len(members)} members of class {class_id} under block {block_oid}")
        return members

    async def get_class_property_descriptors(self, class_manager_oid: int, class_id: List[int]) -> List[Property]:
        """
        Get the property descriptors of a control class from the class manager.

        Args:
            class_manager_oid: Object id of the NcClassManager
            class_id: Control class id to describe

        Returns:
            List of Property descriptors (with value unset)
        """
        commands = [{
            "oid": class_manager_oid,
            "methodId": {
                "level": ClassLevel.NC_CLASS_MANAGER,
                "index": MethodId.GET_CLASS_DESCRIPTOR
            },
            "arguments": {
                "classId": class_id,
                "includeInherited": True
            }
        }]

        responses = await self._send_command(commands)
        response = responses[0]

        if "error" in response:
            raise Exception(f"GetControlClass failed: {response['error']}")

        try:
            properties_data = response['result']['value']['properties']
        except KeyError as e:
            raise Exception(f"Unexpected class descriptor response for class {class_id}: missing key {e}") from e

        properties = [
            Property(
                description=property_data.get("description"),
                id=property_data.get("id"),
                name=property_data.get("name"),
                typeName=property_data.get("typeName"),
                isReadOnly=property_data.get("isReadOnly"),
                isNullable=property_data.get("isNullable"),
                isSequence=property_data.get("isSequence"),
                constraints=property_data.get("constraints"),
                isDeprecated=property_data.get("isDeprecated")
            )
            for property_data in properties_data
        ]

        logger.info(f"Found {len(properties)} properties for class {class_id}")
        return properties

    async def get_properties(self, block: BlockMember) -> Dict[str, Any]:
        """
        Get multiple properties at once
        
        Args:
            block: BlockMember Object to get property values for

        Returns:
            Dictionary mapping property names to values
        """
            
        # Build batch command
        commands = []
        for prop in block.properties:
            commands.append({
                "oid": block.oid,
                "methodId": {
                    "level": ClassLevel.NC_OBJECT,
                    "index": MethodId.GET
                },
                "arguments": {
                    "id": {
                        "level": prop.id['level'],
                        "index": prop.id['index']
                    }
                }
            })
            
        responses = await self._send_command(commands)

        result = {}
        for i, response in enumerate(responses):
            prop = block.properties[i]
        
            if "error" in response:
                logger.warning(f"Error fetching {prop.name}: {response['error']}")
                result[prop.name] = {"error": response["error"]}
                prop.value = {"error": response["error"]}
            else:
                value = response.get("result", {}).get("value")
                result[prop.name] = value
                prop.value = value
    
        logger.info(f"Fetched {len(result)} properties for block {block.role}")
        return result

class DeviceNavigator:
    """
    Helper class for navigating IS-12 device structure
    """

    def __init__(self, client: IS12Client):
        self.client = client
        self.class_manager_oid = None

    async def init(self):
        """Locate the class manager, needed to describe monitor classes."""
        managers = await self.client.find_members_by_class_id(
            ROOT_BLOCK_OID, NC_CLASS_MANAGER_CLASS_ID
        )
        if not managers:
            raise Exception("No NcClassManager found in the device model")
        self.class_manager_oid = managers[0].oid

    async def get_all_monitors(self) -> List[BlockMember]:
        """
        Discover every BCP-008 status monitor in the device model and attach
        their property descriptors.

        Returns:
            List of monitor BlockMember objects with populated properties
        """
        monitors = await self.client.find_members_by_class_id(
            ROOT_BLOCK_OID, NC_STATUS_MONITOR_CLASS_ID
        )
        if not monitors:
            return []

        # Fetch each distinct monitor class descriptor exactly once.
        distinct_classes = {tuple(m.class_id): m.class_id for m in monitors}
        descriptor_lists = await asyncio.gather(*[
            self.client.get_class_property_descriptors(self.class_manager_oid, class_id)
            for class_id in distinct_classes.values()
        ])
        properties_by_class = dict(zip(distinct_classes.keys(), descriptor_lists))

        # Give every monitor its own Property instances so per-instance values
        # (and notification updates) don't leak across shared descriptors.
        for monitor in monitors:
            monitor.properties = [
                replace(prop) for prop in properties_by_class[tuple(monitor.class_id)]
            ]

        return monitors
