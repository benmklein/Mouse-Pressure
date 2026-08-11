set(kritatoolsmousepressure_SOURCES
    tool_mouse_pressure.cpp
    kis_tool_mouse_pressure.cpp
    perfect_freehand_outline.cpp
    mouse_pressure_ink_filter.cpp
    mouse_pressure_icons.qrc
)

kis_add_library(kritatoolsmousepressure MODULE ${kritatoolsmousepressure_SOURCES})
target_link_libraries(kritatoolsmousepressure kritaui)
install(TARGETS kritatoolsmousepressure DESTINATION ${KRITA_PLUGIN_INSTALL_DIR})
install(
    FILES
        mouse_pressure_mouse.png
        dark_mouse_pressure_mouse.png
        light_mouse_pressure_mouse.png
    DESTINATION ${KDE_INSTALL_DATADIR}/krita/pics
)
install(
    FILES mouse_pressure_brush.action
    DESTINATION ${KDE_INSTALL_DATADIR}/krita/actions
)
