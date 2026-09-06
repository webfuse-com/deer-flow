import { ChatProviders } from "@/components/workspace/chats/chat-providers";

export default function SharedThreadLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return <ChatProviders>{children}</ChatProviders>;
}
