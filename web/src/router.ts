import { createRouter, createWebHashHistory } from "vue-router";

import AgentsView from "@/views/AgentsView.vue";
import ChatView from "@/views/ChatView.vue";
import ConnectionsView from "@/views/ConnectionsView.vue";

export const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: "/", name: "chat", component: ChatView },
    { path: "/connections", name: "connections", component: ConnectionsView },
    { path: "/agents", name: "agents", component: AgentsView },
    { path: "/:pathMatch(.*)*", redirect: "/" },
  ],
});
