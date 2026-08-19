// QS-Z90: emit OFIQ's own internal preprocessing geometry (detected faces and
// ADNet-98 landmarks) for one image, as JSON on stdout.
//
// Built from this single translation unit against the REGISTERED OFIQ v1.1.2
// runtime (`libofiq_lib.so`) and the registered source tag's public headers by
// scripts/build_ofiq_landmark_app.sh. No engine logic is reimplemented: the
// registered library performs the full assessment (vectorQuality) and the
// preprocessing results are read back through its public
// vectorQualityWithPreprocessingResults API with the Faces|Landmarks mask.
// These are OFIQ's landmarks — distinct from the matcher pipeline's SCRFD
// five-point detection — and QualSight labels them as such.

#include <cstdint>
#include <iostream>
#include <string>

#include "ofiq_lib.h"
#include "image_io.h"

int main(int argc, char* argv[])
{
    if (argc != 5 || std::string(argv[1]) != "-c" || std::string(argv[3]) != "-i")
    {
        std::cerr << "usage: OFIQLandmarkApp -c <config.jaxn> -i <image>" << std::endl;
        return 2;
    }
    const std::string configPath(argv[2]);
    const std::string imagePath(argv[4]);
    const auto slash = configPath.find_last_of('/');
    const std::string configDir = slash == std::string::npos ? std::string(".") : configPath.substr(0, slash);
    const std::string configFile = slash == std::string::npos ? configPath : configPath.substr(slash + 1);

    auto impl = OFIQ::Interface::getImplementation();
    if (auto status = impl->initialize(configDir, configFile); status.code != OFIQ::ReturnCode::Success)
    {
        std::cerr << "initialize failed: " << status.info << std::endl;
        return 1;
    }
    int major = 0, minor = 0, patch = 0;
    impl->getVersion(major, minor, patch);

    OFIQ::Image image;
    if (auto status = OFIQ_LIB::readImage(imagePath, image); status.code != OFIQ::ReturnCode::Success)
    {
        std::cerr << "readImage failed: " << status.info << std::endl;
        return 1;
    }

    OFIQ::FaceImageQualityAssessment assessment;
    OFIQ::FaceImageQualityPreprocessingResult preprocessing;
    const auto mask = static_cast<std::uint32_t>(OFIQ::PreprocessingResultType::Faces) |
                      static_cast<std::uint32_t>(OFIQ::PreprocessingResultType::Landmarks);
    if (auto status = impl->vectorQualityWithPreprocessingResults(image, assessment, preprocessing, mask);
        status.code != OFIQ::ReturnCode::Success)
    {
        std::cerr << "assessment failed: " << status.info << std::endl;
        return 1;
    }

    std::cout << "{\"engine\":\"OFIQ\",\"version\":\"" << major << "." << minor << "." << patch << "\"";
    std::cout << ",\"image\":{\"width\":" << image.width << ",\"height\":" << image.height << "}";
    std::cout << ",\"landmark_type\":\""
              << (preprocessing.m_landmarks.type == OFIQ::LandmarkType::LM_98 ? "ADNET_LM_98" : "NOT_SET")
              << "\"";
    std::cout << ",\"faces\":[";
    for (std::size_t i = 0; i < preprocessing.m_faces.size(); ++i)
    {
        const auto& box = preprocessing.m_faces[i];
        if (i) std::cout << ",";
        std::cout << "{\"x\":" << box.xleft << ",\"y\":" << box.ytop << ",\"width\":" << box.width
                  << ",\"height\":" << box.height << "}";
    }
    std::cout << "],\"landmarks\":[";
    for (std::size_t i = 0; i < preprocessing.m_landmarks.landmarks.size(); ++i)
    {
        const auto& point = preprocessing.m_landmarks.landmarks[i];
        if (i) std::cout << ",";
        std::cout << "[" << point.x << "," << point.y << "]";
    }
    std::cout << "]}" << std::endl;
    return 0;
}
