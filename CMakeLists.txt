# Host build of the CW timing chain. No cross-compiler, no linker script.
#
# Compiles the real app/cwkeyer.c, app/cwmacro.c and app/cwhardware.c against
# stub drivers, so the tests exercise firmware code rather than a reimplementation.
# The one exception is CW_ReadKeysForMode / pin configuration in cwhardware.c,
# which read GPIO directly -- those are replaced, and the debounce above them is
# not (it is part of the timing behaviour under test).

cmake_minimum_required(VERSION 3.20)
project(uvk5_sim C)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)

# Path to the firmware checkout. Override with -DFIRMWARE_DIR=... if the layout
# differs from the sibling clone this defaults to.
set(FIRMWARE_DIR "${CMAKE_CURRENT_SOURCE_DIR}/../uvk5-sat" CACHE PATH
    "Firmware source tree containing App/")
if(NOT EXISTS "${FIRMWARE_DIR}/App/app/cwkeyer.c")
    message(FATAL_ERROR
        "FIRMWARE_DIR does not look like the firmware tree: ${FIRMWARE_DIR}\n"
        "Pass -DFIRMWARE_DIR=/path/to/uvk5-sat")
endif()

add_library(sim_harness STATIC
    harness/sim_clock.c
    harness/sim_paddle.c
    harness/sim_record.c
    harness/sim_keyer.c
    stubs/driver_stubs.c
    stubs/cwhardware_stub.c
    stubs/firmware_globals.c
)

# Firmware sources compiled as-is.
target_sources(sim_harness PRIVATE
    ${FIRMWARE_DIR}/App/app/cwkeyer.c
    ${FIRMWARE_DIR}/App/app/cwmacro.c
)

target_include_directories(sim_harness PUBLIC
    ${CMAKE_CURRENT_SOURCE_DIR}
    ${CMAKE_CURRENT_SOURCE_DIR}/shim      # replaces MCU headers
    ${FIRMWARE_DIR}/App
)

# ENABLE_CW_MODULATOR selects the CW code paths; the rest stay off so the stub
# surface remains small. CW_KEYER_DEBUG routes the firmware's own tracing into
# the recorder, which is useful when a scenario fails.
target_compile_definitions(sim_harness PUBLIC
    ENABLE_CW_MODULATOR
    ENABLE_FEAT_NR7Y_CW
)

target_compile_options(sim_harness PRIVATE
    -Wall -Wextra -Wno-unused-parameter
    -g -O1
    -fsanitize=address,undefined
)
target_link_options(sim_harness PUBLIC -fsanitize=address,undefined)

enable_testing()

# Each scenario file is its own executable so a crash in one does not hide the
# rest, and so ASan reports point at a single scenario.
file(GLOB SIM_TESTS ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_*.c)
foreach(test_src ${SIM_TESTS})
    get_filename_component(test_name ${test_src} NAME_WE)
    add_executable(${test_name} ${test_src})
    target_link_libraries(${test_name} PRIVATE sim_harness)
    target_compile_options(${test_name} PRIVATE
        -Wall -Wextra -g -O1 -fsanitize=address,undefined)
    add_test(NAME ${test_name} COMMAND ${test_name})
endforeach()
