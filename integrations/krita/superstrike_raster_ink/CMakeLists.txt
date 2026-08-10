set(kritatoolsuperstrikeink_SOURCES
    tool_superstrike_ink.cpp
    kis_tool_superstrike_ink.cpp
    perfect_freehand_outline.cpp
    superstrike_ink_filter.cpp
    superstrike_icons.qrc
)

kis_add_library(kritatoolsuperstrikeink MODULE ${kritatoolsuperstrikeink_SOURCES})
target_link_libraries(kritatoolsuperstrikeink kritaui)
install(TARGETS kritatoolsuperstrikeink DESTINATION ${KRITA_PLUGIN_INSTALL_DIR})
install(
    FILES
        superstrike_mouse.png
        dark_superstrike_mouse.png
        light_superstrike_mouse.png
    DESTINATION ${KDE_INSTALL_DATADIR}/krita/pics
)
install(
    FILES superstrike_raster_ink.action
    DESTINATION ${KDE_INSTALL_DATADIR}/krita/actions
)
