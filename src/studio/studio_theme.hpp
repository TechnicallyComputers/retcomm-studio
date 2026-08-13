#pragma once

// RetComM Studio visual tokens — CRT violet base (hub / recomp-ui), teal Studio accent.

#include "imgui.h"

namespace retcomm::studio {

struct Theme {
    ImVec4 background;
    ImVec4 background2;
    ImVec4 panel;
    ImVec4 panel_hovered;
    ImVec4 control;
    ImVec4 control_hovered;
    ImVec4 border;
    ImVec4 accent;
    ImVec4 accent_dim;
    ImVec4 accent_text;
    ImVec4 accent_button;
    ImVec4 accent_button_hovered;
    ImVec4 accent_button_active;
    ImVec4 text;
    ImVec4 text_muted;
    ImVec4 good;
    ImVec4 warn;
    ImVec4 bad;
    ImVec4 focus;
    float spacing_sm = 8.f;
    float spacing_md = 16.f;
    float spacing_lg = 24.f;
    float radius_sm = 6.f;
    float radius_lg = 14.f;
};

inline Theme crt_theme() {
    Theme t;
    t.background = ImVec4(0.039f, 0.051f, 0.086f, 1.f);       // #0A0D16
    t.background2 = ImVec4(0.071f, 0.090f, 0.145f, 1.f);      // #121725
    t.panel = ImVec4(0.078f, 0.102f, 0.157f, 1.f);            // #141A28
    t.panel_hovered = ImVec4(0.125f, 0.165f, 0.243f, 1.f);    // #202A3E
    t.control = ImVec4(0.106f, 0.137f, 0.208f, 1.f);          // #1B2335
    t.control_hovered = ImVec4(0.145f, 0.188f, 0.278f, 1.f);  // #253047
    t.border = ImVec4(0.169f, 0.208f, 0.314f, 1.f);            // #2B3550
    t.accent = ImVec4(0.220f, 0.780f, 0.820f, 1.f);            // Studio teal
    t.accent_dim = ImVec4(0.140f, 0.520f, 0.560f, 1.f);
    t.accent_text = ImVec4(1.f, 1.f, 1.f, 1.f);
    t.accent_button = ImVec4(0.120f, 0.420f, 0.460f, 1.f);
    t.accent_button_hovered = ImVec4(0.160f, 0.520f, 0.560f, 1.f);
    t.accent_button_active = ImVec4(0.090f, 0.340f, 0.380f, 1.f);
    t.text = ImVec4(0.925f, 0.933f, 0.965f, 1.f);
    t.text_muted = ImVec4(0.529f, 0.565f, 0.659f, 1.f);
    t.good = ImVec4(0.275f, 0.890f, 0.608f, 1.f);
    t.warn = ImVec4(0.961f, 0.698f, 0.235f, 1.f);
    t.bad = ImVec4(0.920f, 0.320f, 0.380f, 1.f);
    t.focus = ImVec4(0.220f, 0.882f, 0.902f, 1.f);
    return t;
}

inline void apply_imgui_style(const Theme& t) {
    ImGuiStyle& s = ImGui::GetStyle();
    s.WindowRounding = t.radius_lg;
    s.ChildRounding = t.radius_lg;
    s.FrameRounding = t.radius_sm;
    s.PopupRounding = t.radius_sm;
    s.ScrollbarRounding = t.radius_sm;
    s.GrabRounding = t.radius_sm;
    s.TabRounding = t.radius_sm;
    s.WindowBorderSize = 0.f;
    s.FrameBorderSize = 1.f;
    s.ItemSpacing = ImVec2(t.spacing_sm, t.spacing_sm);
    s.ItemInnerSpacing = ImVec2(t.spacing_sm, t.spacing_sm);
    s.WindowPadding = ImVec2(t.spacing_md, t.spacing_md);
    s.FramePadding = ImVec2(12.f, 8.f);

    ImVec4* c = s.Colors;
    c[ImGuiCol_WindowBg] = t.background;
    c[ImGuiCol_ChildBg] = ImVec4(0, 0, 0, 0);
    c[ImGuiCol_PopupBg] = t.panel;
    c[ImGuiCol_Border] = t.border;
    c[ImGuiCol_FrameBg] = t.control;
    c[ImGuiCol_FrameBgHovered] = t.control_hovered;
    c[ImGuiCol_FrameBgActive] = t.accent_dim;
    c[ImGuiCol_TitleBg] = t.background2;
    c[ImGuiCol_TitleBgActive] = t.background2;
    c[ImGuiCol_Button] = t.control;
    c[ImGuiCol_ButtonHovered] = t.control_hovered;
    c[ImGuiCol_ButtonActive] = t.accent_dim;
    c[ImGuiCol_Header] = t.panel;
    c[ImGuiCol_HeaderHovered] = t.panel_hovered;
    c[ImGuiCol_HeaderActive] = t.accent_dim;
    c[ImGuiCol_Text] = t.text;
    c[ImGuiCol_TextDisabled] = t.text_muted;
    c[ImGuiCol_TextLink] = t.accent;
    c[ImGuiCol_CheckMark] = t.accent;
    c[ImGuiCol_SliderGrab] = t.accent;
    c[ImGuiCol_SliderGrabActive] = t.focus;
    c[ImGuiCol_Separator] = t.border;
    // Active tab: solid teal-tinted fill + accent overline (persistent, not hover-only).
    c[ImGuiCol_Tab] = t.background2;
    c[ImGuiCol_TabHovered] = t.panel_hovered;
    c[ImGuiCol_TabSelected] = t.accent_button;
    c[ImGuiCol_TabSelectedOverline] = t.accent;
    c[ImGuiCol_TabDimmed] = t.background2;
    c[ImGuiCol_TabDimmedSelected] = t.accent_dim;
    c[ImGuiCol_TableHeaderBg] = t.background2;
    c[ImGuiCol_TableBorderStrong] = t.border;
    c[ImGuiCol_TableBorderLight] = t.border;
    c[ImGuiCol_TableRowBg] = ImVec4(0, 0, 0, 0);
    c[ImGuiCol_TableRowBgAlt] = ImVec4(1, 1, 1, 0.02f);
    c[ImGuiCol_ScrollbarBg] = t.background2;
    c[ImGuiCol_ScrollbarGrab] = t.control_hovered;
    c[ImGuiCol_ScrollbarGrabHovered] = t.border;
    c[ImGuiCol_ScrollbarGrabActive] = t.accent_dim;
}

} // namespace retcomm::studio
