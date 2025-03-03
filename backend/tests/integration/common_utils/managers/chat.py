import json
from datetime import datetime
from urllib.parse import urlencode
from uuid import UUID

import requests
from requests.models import Response

from ee.onyx.server.query_history.models import ChatSessionMinimal
from onyx.configs.constants import QAFeedbackType
from onyx.context.search.models import RetrievalDetails
from onyx.file_store.models import FileDescriptor
from onyx.llm.override_models import LLMOverride
from onyx.llm.override_models import PromptOverride
from onyx.server.documents.models import PaginatedReturn
from onyx.server.query_and_chat.models import ChatSessionCreationRequest
from onyx.server.query_and_chat.models import CreateChatMessageRequest
from tests.integration.common_utils.constants import API_SERVER_URL
from tests.integration.common_utils.constants import GENERAL_HEADERS
from tests.integration.common_utils.test_models import DATestChatMessage
from tests.integration.common_utils.test_models import DATestChatSession
from tests.integration.common_utils.test_models import DATestUser
from tests.integration.common_utils.test_models import StreamedResponse


class ChatSessionManager:
    @staticmethod
    def create(
        persona_id: int = 0,
        description: str = "Test chat session",
        user_performing_action: DATestUser | None = None,
    ) -> DATestChatSession:
        chat_session_creation_req = ChatSessionCreationRequest(
            persona_id=persona_id, description=description
        )
        response = requests.post(
            f"{API_SERVER_URL}/chat/create-chat-session",
            json=chat_session_creation_req.model_dump(),
            headers=user_performing_action.headers
            if user_performing_action
            else GENERAL_HEADERS,
        )
        response.raise_for_status()
        chat_session_id = response.json()["chat_session_id"]
        return DATestChatSession(
            id=chat_session_id, persona_id=persona_id, description=description
        )

    @staticmethod
    def send_message(
        chat_session_id: UUID,
        message: str,
        parent_message_id: int | None = None,
        user_performing_action: DATestUser | None = None,
        file_descriptors: list[FileDescriptor] = [],
        prompt_id: int | None = None,
        search_doc_ids: list[int] | None = None,
        retrieval_options: RetrievalDetails | None = None,
        query_override: str | None = None,
        regenerate: bool | None = None,
        llm_override: LLMOverride | None = None,
        prompt_override: PromptOverride | None = None,
        alternate_assistant_id: int | None = None,
        use_existing_user_message: bool = False,
    ) -> StreamedResponse:
        chat_message_req = CreateChatMessageRequest(
            chat_session_id=chat_session_id,
            parent_message_id=parent_message_id,
            message=message,
            file_descriptors=file_descriptors or [],
            prompt_id=prompt_id,
            search_doc_ids=search_doc_ids or [],
            retrieval_options=retrieval_options,
            rerank_settings=None,  # Can be added if needed
            query_override=query_override,
            regenerate=regenerate,
            llm_override=llm_override,
            prompt_override=prompt_override,
            alternate_assistant_id=alternate_assistant_id,
            use_existing_user_message=use_existing_user_message,
        )

        response = requests.post(
            f"{API_SERVER_URL}/chat/send-message",
            json=chat_message_req.model_dump(),
            headers=user_performing_action.headers
            if user_performing_action
            else GENERAL_HEADERS,
            stream=True,
        )

        return ChatSessionManager.analyze_response(response)

    @staticmethod
    def analyze_response(response: Response) -> StreamedResponse:
        response_data = [
            json.loads(line.decode("utf-8")) for line in response.iter_lines() if line
        ]

        analyzed = StreamedResponse()

        for data in response_data:
            if "rephrased_query" in data:
                analyzed.rephrased_query = data["rephrased_query"]
            elif "tool_name" in data:
                analyzed.tool_name = data["tool_name"]
                analyzed.tool_result = (
                    data.get("tool_result")
                    if analyzed.tool_name == "run_search"
                    else None
                )
            elif "relevance_summaries" in data:
                analyzed.relevance_summaries = data["relevance_summaries"]
            elif "answer_piece" in data and data["answer_piece"]:
                analyzed.full_message += data["answer_piece"]

        return analyzed

    @staticmethod
    def get_chat_history(
        chat_session: DATestChatSession,
        user_performing_action: DATestUser | None = None,
    ) -> list[DATestChatMessage]:
        response = requests.get(
            f"{API_SERVER_URL}/chat/get-chat-session/{chat_session.id}",
            headers=user_performing_action.headers
            if user_performing_action
            else GENERAL_HEADERS,
        )
        response.raise_for_status()

        return [
            DATestChatMessage(
                id=msg["message_id"],
                chat_session_id=chat_session.id,
                parent_message_id=msg.get("parent_message"),
                message=msg["message"],
            )
            for msg in response.json()["messages"]
        ]

    @staticmethod
    def get_chat_session_history(
        user_performing_action: DATestUser,
        page_size: int,
        page_num: int,
        feedback_type: QAFeedbackType | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
    ) -> PaginatedReturn[ChatSessionMinimal]:
        query_params = {
            "page_num": page_num,
            "page_size": page_size,
            "feedback_type": feedback_type if feedback_type else None,
            "start_time": start_time if start_time else None,
            "end_time": end_time if end_time else None,
        }
        # Remove None values
        query_params = {
            key: value for key, value in query_params.items() if value is not None
        }

        response = requests.get(
            f"{API_SERVER_URL}/admin/chat-session-history?{urlencode(query_params, doseq=True)}",
            headers=user_performing_action.headers,
        )
        response.raise_for_status()
        data = response.json()
        return PaginatedReturn(
            items=[
                ChatSessionMinimal(**chat_session) for chat_session in data["items"]
            ],
            total_items=data["total_items"],
        )
