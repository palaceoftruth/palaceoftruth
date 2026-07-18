import { lazy } from "react";
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import Layout from "./components/Layout";
import Dashboard from "./pages/Dashboard";

const Ingest = lazy(() => import("./pages/Ingest"));
const Search = lazy(() => import("./pages/Search"));
const Chat = lazy(() => import("./pages/Chat"));
const Browse = lazy(() => import("./pages/Browse"));
const SavedWeb = lazy(() => import("./pages/SavedWeb"));
const Graph = lazy(() => import("./pages/Graph"));
const Settings = lazy(() => import("./pages/Settings"));
const ItemDetail = lazy(() => import("./pages/ItemDetail"));
const ApiDocs = lazy(() => import("./pages/ApiDocs"));
const Feeds = lazy(() => import("./pages/Feeds"));
const Sources = lazy(() => import("./pages/Sources"));
const Palace = lazy(() => import("./pages/Palace"));
const PalaceControlTower = lazy(() => import("./pages/PalaceControlTower"));
const PalaceReviewInbox = lazy(() => import("./pages/PalaceReviewInbox"));
const OAuthConsent = lazy(() => import("./pages/OAuthConsent"));

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="oauth/consent" element={<OAuthConsent />} />
        <Route path="/" element={<Layout />}>
          <Route index element={<Dashboard />} />
          <Route path="ingest" element={<Ingest />} />
          <Route path="search" element={<Search />} />
          <Route path="chat" element={<Chat />} />
          <Route path="browse" element={<Browse />} />
          <Route path="saved-web" element={<SavedWeb />} />
          <Route path="items/:id" element={<ItemDetail />} />
          <Route path="sources" element={<Sources />} />
          <Route path="feeds" element={<Feeds />} />
          <Route path="palace" element={<Palace />} />
          <Route path="palace/control-tower" element={<PalaceControlTower />} />
          <Route path="palace/review-inbox" element={<PalaceReviewInbox />} />
          <Route path="graph" element={<Graph />} />
          <Route path="settings" element={<Settings />} />
          <Route path="api-docs" element={<ApiDocs />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Routes>
    </BrowserRouter>
  );
}
