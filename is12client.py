import asyncio
import json
import logging
from typing import Dict, List, Optional, Any, Callable, Tuple
from dataclasses import dataclass
from enum import IntEnum
import websockets
from websockets.client import WebSocketClientProtocol


logger = logging.getLogger(__name__)


class MessageType(IntEnum):
    """IS-12 Message Types"""
    COMMAND = 0
    COMMAND_RESPONSE = 1
    NOTIFICATION = 2


class MethodId(IntEnum):
    """
    These are the '1mX' identifiers 
    """
    # 1m1
    GET = 1
    # 1m2
    SET = 2
    # 1m3
    GET_SEQUENCE_ITEM = 3
    # 1m4
    SET_SEQUENCE_ITEM = 4
    # 1m5
    ADD_SEQUENCE_ITEM = 5
    # 1m6
    REMOVE_SEQUENCE_ITEM = 6
    # 1m7
    GET_SEQUENCE_LENGTH = 7
    # 1m8
    GET_SEQUENCE_VALUES = 8 

class RoleLevel(IntEnum):
    """
    These are the '2mX' identifirs 
    """
    # 2m1
    GET_MEMBER_DESCRIPTORS = 1
    # 2m2
    FIND_MEMBERS_BY_PATH = 2
    # 2m3
    FIND_MEMBERS_BY_ROLE = 3
    # 2m4
    FIND_MEMBERS_BY_CLASS_ID = 4

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
    value = None

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
        self.ws: Optional[WebSocketClientProtocol] = None
        self._handle_counter = 0
        self._pending_requests: Dict[int, asyncio.Future] = {}
        self._subscriptions: Dict[Tuple[int, int], Callable] = {}
        self._members_cache: Dict[int, List[BlockMember]] = {}
        self._receive_task: Optional[asyncio.Task] = None
        
    async def connect(self):
        """Establish WebSocket connection"""
        if self.ws and self.ws.open:
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
            
    async def _handle_response(self, message: Dict):
        """Handle command responses"""
        responses = message.get("responses", [])
        
        for response in responses:
            handle = response.get("handle")
            if handle in self._pending_requests:
                future = self._pending_requests.pop(handle)
                if not future.cancelled():
                    future.set_result(response)
                    
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
    
    async def get_block_members(self, block_oid: int, use_cache: bool = True) -> List[BlockMember]:
        """
        Get members of a block (children objects)
        
        Args:
            block_oid: Block object identifier
            use_cache: Use cached result if available
            
        Returns:
            List of BlockMember objects
        """
        if use_cache and block_oid in self._members_cache:
            return self._members_cache[block_oid]
            
        commands = [{
            "oid": block_oid,
            "methodId": {
                "level": RoleLevel.GET_MEMBER_DESCRIPTORS,
                "index": MethodId.GET
            },
            "arguments": {
                "id": {
                    "level": 2,
                    "index": 2  # members property (2p2)
                }
            }
        }]
        
        responses = await self._send_command(commands)
        response = responses[0]
        
        if "error" in response:
            raise Exception(f"Get members failed: {response['error']}")
            
        result = response.get("result", {})
        members_data = result.get("value", [])
        
        members = []
        for member_data in members_data:
            member = BlockMember(
                oid=member_data.get("oid"),
                role=member_data.get("role", ""),
                class_id=member_data.get("classId", []),
                user_label=member_data.get("userLabel", ""),
                properties=[],
                description=member_data.get("description", ""),
                constant_oid=member_data.get("constantOid", True),
                owner=member_data.get("owner", block_oid)
            )
            members.append(member)
            
        self._members_cache[block_oid] = members
        logger.info(f"Found {len(members)} members in block {block_oid}")
        return members
    
    async def gather_block_properties(self, class_manager_oid: int, block: BlockMember) -> List[Property]:
        """
        Gets all properties for a BlockMember object

        Args: 
            class_manager_oid: The object id of the class manager
            block: The BlockMember object to use for Property lookup

        Returns: 
            List of Property Objects that is attached to BlockMember
        """
        commands = [{
            "oid": class_manager_oid,
            "methodId": {
                "level": RoleLevel.FIND_MEMBERS_BY_ROLE,
                "index": MethodId.GET
            },
            "arguments": {
                "classId": block.class_id, 
                "includeInherited": True
            }
        }]
        
        responses = await self._send_command(commands)
        response = responses[0]

        if "error" in response:
            raise Exception(f"Get members failed: {response['error']}")
        
        properties_data = response['result']['value']['properties']

        properties = []
        for property_data in properties_data:
            prop = Property(
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
            properties.append(prop)

        block.properties = properties

        logger.info(f"Found {len(properties)} properties in block {block.description}")
        
        return properties 
     
    def find_member_by_role(self, members: List[BlockMember], role: str) -> Optional[BlockMember]:
        """
        Find a member by its role name
        
        Args:
            members: List of members to search
            role: Role name to find
            
        Returns:
            BlockMember or None if not found
        """
        for member in members:
            if member.role == role:
                return member
        return None
    
    def find_members_by_class(self, members: List[BlockMember], class_id: List[int]) -> List[BlockMember]:
        """
        Find all members with a specific class ID
        
        Args:
            members: List of members to search
            class_id: Class ID to match
            
        Returns:
            List of matching BlockMembers
        """
        return [m for m in members if m.class_id == class_id]
        
    async def get_property(self, block: BlockMember, property_name: str) -> Any:
        """
        Get a single property value
        
        Args:
            block: BlockMember object to get property for
            
        Returns:
            Property value or None
        """
        found_prop = None
        for prop in block.properties:
            if property_name == prop.name:
                found_prop = prop
                break
        
        if not found_prop:
            logger.warning(f"Could not find property: {property_name} in block: {block.role}") 
            return None

        level = found_prop.id['level']
        index = found_prop.id['index']

        commands = [{
            "oid": block.oid,
            "methodId": {
                "level": RoleLevel.GET_MEMBER_DESCRIPTORS,
                "index": MethodId.GET
            },
            "arguments": {
                "id": {
                    "level": level,
                    "index": index
                }
            }
        }]
        
        responses = await self._send_command(commands)
        response = responses[0]
        
        if "error" in response:
            raise Exception(f"GetMember failed: {response['error']}")
            
        return response.get("result", {}).get("value")
        
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
                    "level": RoleLevel.GET_MEMBER_DESCRIPTORS,
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
        self.root_members = None 
        self.class_manager = None

    async def init(self):
        await self._get_root_members()
        await self._get_class_manager()
        
    async def _get_root_members(self):
        if self.root_members == None: 
            self.root_members = await self.client.get_block_members(1)

        return self.root_members

    async def _get_class_manager(self):
        if self.class_manager == None:        
            self.class_manager = self.client.find_member_by_role(self.root_members, "ClassManager")

        return self.class_manager

    async def get_sender_monitors(self):
        """
        Discovers Sender Monitor Blocks by class id and gathers properties for each Block

        Returns:
            List of Sender Monitor BlockMember Objects
        """
        sender_block = self.client.find_member_by_role(self.root_members, "senders")

        sender_block_members = await self.client.get_block_members(sender_block.oid)
        sender_monitors = self.client.find_members_by_class(sender_block_members, [1, 2, 2, 2])

        # Gather properties for all monitors in parallel
        tasks = [
            self.client.gather_block_properties(self.class_manager.oid, monitor)
            for monitor in sender_monitors
        ]
        await asyncio.gather(*tasks)

        return sender_monitors

    async def get_receiver_monitors(self):
        """
        Discovers Receiver Monitor Blocks by Class ID and gathers properties for each Block
        
        Returns:
            List of Receiver Monitor BlockMember objects
        """
        receiver_block = self.client.find_member_by_role(self.root_members, "receivers")

        receiver_block_members = await self.client.get_block_members(receiver_block.oid)
        receiver_monitors = self.client.find_members_by_class(receiver_block_members, [1, 2, 2, 1])

        # Gather properties for all monitors in parallel
        tasks = [
            self.client.gather_block_properties(self.class_manager.oid, monitor)
            for monitor in receiver_monitors
        ]
        await asyncio.gather(*tasks)

        return receiver_monitors

    async def get_all_monitors(self):
        """
        Discovers Monitor Blocks by Class ID and gathers properties for each Block
        
        Returns:
            List of all Monitor BlockMember objects
        """
        sender_monitors = await self.get_sender_monitors()
        receiver_monitors = await self.get_receiver_monitors()

        return sender_monitors + receiver_monitors
